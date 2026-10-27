"""
Handler oficial do evento `PreToolUse` do Claude Code (Partes 2 e 3 do CATO).
Intercepta:
1. `Read`: Deduplicação SHA-256 por sessão + Esqueletonização AST automática para arquivos >300 linhas
2. `Grep`: Injeta `head_limit: 60` via `updatedInput` quando omitido para evitar explosão de resultados
3. `Bash`: Encadeia com `rtk` (se presente) ou envolve comandos verbosos no `cato exec` via `updatedInput`,
   somente quando as regras de permissão do usuário já aprovariam o comando original
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from cato.code_intel.dedup import DEFAULT_SKELETON_LINE_THRESHOLD, evaluate_read_request
from cato.hooks.bash_permissions import is_preapproved_bash_command
from cato.storage.db import CatoDB

# Comandos interativos ou triviais que NUNCA devem ser envolvidos pelo wrapper
SKIP_BASH_PREFIXES = (
    "rtk ",
    "cato ",
    "cd ",
    "mkdir ",
    "rm ",
    "mv ",
    "cp ",
    "touch ",
    "chmod ",
    "chown ",
    "export ",
    "set ",
    "echo ",
    "pwd",
    "which ",
    "where ",
    "kill ",
    "git add",
    "git commit",
    "git checkout",
    "git switch",
    "git branch",
    "git push",
    "git pull",
    "git fetch",
    "git clone",
    "git stash",
    "git rm",
    "git mv",
)

# Padrões de comandos que geram saídas potencialmente grandes e se beneficiam do `cato exec`
OPTIMIZABLE_BASH_KEYWORDS = (
    "pytest",
    "py.test",
    "unittest",
    "jest",
    "vitest",
    "mocha",
    "cargo test",
    "cargo check",
    "cargo build",
    "go test",
    "go build",
    "npm test",
    "npm run ",
    "pnpm test",
    "pnpm run ",
    "yarn test",
    "yarn build",
    "tsc",
    "eslint",
    "ruff",
    "flake8",
    "mypy",
    "pyright",
    "clippy",
    "git status",
    "git diff",
    "git log",
    "git show",
    "docker logs",
    "docker compose logs",
    "kubectl logs",
    "pip install",
    "npm install",
    "pnpm install",
    "find ",
    "grep ",
    "rg ",
    "dir ",
    "ls ",
    "tree ",
    "python ",
    "python3 ",
    "py ",
    "node ",
)


def get_cato_executable_cmd() -> str:
    """Retorna o comando para invocar a CLI do CATO de forma confiável em Bash, PowerShell e CMD."""
    src_dir = Path(__file__).resolve().parents[2]
    py_exe = str(Path(sys.executable).resolve()).replace("\\", "/")
    cli_script = str((src_dir / "cato" / "cli.py").resolve()).replace("\\", "/")
    py_quoted = f'"{py_exe}"' if " " in py_exe else py_exe
    cli_quoted = f'"{cli_script}"' if " " in cli_script else cli_script
    return f"{py_quoted} {cli_quoted}"


def should_wrap_bash_command(command: str) -> bool:
    """Decide se um comando Bash deve ser envolvido pelo otimizador `cato exec`."""
    cmd_stripped = command.strip()
    if not cmd_stripped:
        return False

    cmd_lower = cmd_stripped.lower()
    if any(cmd_lower.startswith(prefix) for prefix in SKIP_BASH_PREFIXES):
        return False

    # Não envolve comandos com heredoc (<<), scripts compostos com ';', pipes que já filtram (tail/head) ou background (&)
    if (
        "<<" in cmd_stripped
        or ";" in cmd_stripped
        or "| tail" in cmd_lower
        or "| head" in cmd_lower
        or cmd_stripped.endswith("&")
    ):
        return False

    return any(kw in cmd_lower for kw in OPTIMIZABLE_BASH_KEYWORDS)


def try_delegate_to_rtk(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Se o binário `rtk` estiver instalado e o comando for simples (sem pipes/subshells/docker),
    verifica se `rtk hook claude` reescreve o comando e injeta o caminho absoluto do `rtk.exe`
    para que o `/usr/bin/bash` do Claude Desktop nunca falhe com `rtk: command not found`.
    """
    if os.environ.get("CATO_DISABLE_RTK_CHAIN") == "1":
        return None

    cmd_str = str((payload.get("tool_input") or {}).get("command", "")).strip()
    # Não delega ao RTK comandos compostos (; | &&) nem docker/ssh (onde rtk quebra pipelines)
    if any(tok in cmd_str for tok in (";", "|", "&&", "||", "\n", "docker ", "ssh ", ">")):
        return None

    rtk_bin = shutil.which("rtk") or str(Path.home() / ".local" / "bin" / "rtk.exe")
    if not Path(rtk_bin).exists() and not shutil.which("rtk"):
        return None

    rtk_posix = str(Path(rtk_bin).resolve()).replace("\\", "/")

    try:
        proc = subprocess.run(
            [rtk_bin, "hook", "claude"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2.0,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            rtk_resp = json.loads(proc.stdout.strip())
            hook_out = rtk_resp.get("hookSpecificOutput", {})
            new_cmd = hook_out.get("updatedInput", {}).get("command", "")
            if new_cmd:
                if new_cmd.startswith("rtk "):
                    hook_out["updatedInput"]["command"] = f'"{rtk_posix}" ' + new_cmd[4:]
                return rtk_resp
    except Exception:
        pass
    return None


def handle_pre_tool_use(
    payload: Dict[str, Any],
    db: Optional[CatoDB] = None,
) -> Optional[Dict[str, Any]]:
    """
    Processa o payload JSON do evento `PreToolUse` do Claude Code e retorna
    a estrutura `hookSpecificOutput` correspondente (ou `None` para passthrough).
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "default")
    db_instance = db or CatoDB()

    # =========================================================================
    # 1. INTERCEPTAÇÃO DE `Read` (Deduplicação SHA-256 + Esqueleto AST)
    # =========================================================================
    if tool_name == "Read":
        file_path = tool_input.get("file_path") or tool_input.get("path")
        if not file_path:
            return None

        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        threshold = int(os.environ.get("CATO_SKELETON_THRESHOLD", str(DEFAULT_SKELETON_LINE_THRESHOLD)))

        decision = evaluate_read_request(
            file_path=str(file_path),
            offset=int(offset) if offset is not None else None,
            limit=int(limit) if limit is not None else None,
            session_id=session_id,
            skeleton_threshold=threshold,
            db=db_instance,
        )

        if not decision.allow:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.reason or "Blocked by CATO Read Guard",
                }
            }

        if decision.additional_context:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": decision.additional_context,
                }
            }
        return None

    # =========================================================================
    # 2. INTERCEPTAÇÃO DE `Grep` (Limite preventivo para evitar despejo massivo)
    #    Nota: Não registra telemetria estimada/falsa antes da execução porque não
    #    sabemos quantas linhas o Grep retornaria sem o head_limit.
    # =========================================================================
    if tool_name == "Grep":
        head_limit = tool_input.get("head_limit")
        if not head_limit:
            updated_input = dict(tool_input)
            updated_input["head_limit"] = 60
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "CATO injected head_limit=60 to prevent unbounded Grep output",
                    "updatedInput": updated_input,
                }
            }
        return None

    # =========================================================================
    # 3. INTERCEPTAÇÃO DE `Bash` (Encadeamento RTK + Wrapper `cato exec`)
    # =========================================================================
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        if not command:
            return None

        # O Claude Code avalia as regras de permissão contra o comando reescrito; reescrever um comando
        # que o usuário não aprovou esconderia o comando real das regras `deny`/`ask` e do prompt.
        # Nesse caso o comando segue intocado pelo fluxo normal de permissões.
        if not is_preapproved_bash_command(command, payload):
            return None

        # Primeiro verifica se o RTK está ativo e deseja reescrever este comando nativamente
        rtk_result = try_delegate_to_rtk(payload)
        if rtk_result is not None:
            rtk_result.setdefault("hookSpecificOutput", {})["permissionDecision"] = "allow"
            return rtk_result

        # Caso contrário, se for um comando candidato à otimização do CATO, envolve com `cato exec`.
        # O comando vai entre aspas (legível no transcript), não codificado em base64.
        if should_wrap_bash_command(command):
            cato_cmd = get_cato_executable_cmd()
            wrapped_command = f"{cato_cmd} exec --session-id {shlex.quote(str(session_id))} -- {shlex.quote(command)}"

            updated_input = dict(tool_input)
            updated_input["command"] = wrapped_command
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "CATO wrapped a pre-approved command for deterministic output compression",
                    "updatedInput": updated_input,
                }
            }
        return None

    return None


def main_stdio() -> int:
    """Lê o evento `PreToolUse` do stdin e imprime a resposta JSON no stdout."""
    try:
        raw_stdin = sys.stdin.read()
        if not raw_stdin.strip():
            return 0
        payload = json.loads(raw_stdin)
        response = handle_pre_tool_use(payload)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception:
        # Arquitetura fail-open: qualquer erro inesperado no hook nunca bloqueia o Claude Code
        return 0
    return 0
