"""
Handler oficial do evento `PreToolUse` do Claude Code (Partes 2 e 3 do CATO).
Intercepta:
1. `Read` (opt-in, `CATO_READ_GUARD=1`): Deduplicação SHA-256 por sessão + Esqueleto AST para arquivos >300 linhas.
   Fica desligado por padrão: negar a leitura força outro turno, que relê o contexto inteiro, e o
   Claude Code já devolve um aviso curto ao reler um arquivo sem mudanças.
2. `Bash`: delega a reescrita ao RTK (`rtk hook claude`) somente quando o hook global do RTK não está
   instalado (evita processar cada comando duas vezes) e o comando original já é aprovado pelas regras
   do usuário. A compressão de saída do próprio CATO acontece no `PostToolUse`, sobre a saída real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from cato.hooks.bash_permissions import is_preapproved_bash_command, rtk_hook_installed

if TYPE_CHECKING:
    from cato.storage.db import CatoDB


def get_cato_executable_cmd(script: str = "cli.py") -> str:
    """
    Comando para invocar um script do CATO de forma confiável em Bash, PowerShell e CMD.
    Hooks usam `hook_runner.py` (import mínimo); comandos manuais usam `cli.py`.
    `-S` pula o `site` (o CATO não tem dependências) e corta parte do tempo de partida do Python.
    """
    src_dir = Path(__file__).resolve().parents[2]
    py_exe = str(Path(sys.executable).resolve()).replace("\\", "/")
    target = str((src_dir / "cato" / script).resolve()).replace("\\", "/")
    py_quoted = f'"{py_exe}"' if " " in py_exe else py_exe
    target_quoted = f'"{target}"' if " " in target else target
    flags = " -S" if script == "hook_runner.py" else ""
    return f"{py_quoted}{flags} {target_quoted}"


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
    db: Optional["CatoDB"] = None,
) -> Optional[Dict[str, Any]]:
    """
    Processa o payload JSON do evento `PreToolUse` do Claude Code e retorna
    a estrutura `hookSpecificOutput` correspondente (ou `None` para passthrough).
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "default")

    # =========================================================================
    # 1. `Read` (opt-in): Deduplicação SHA-256 + Esqueleto AST
    # =========================================================================
    if tool_name == "Read":
        if os.environ.get("CATO_READ_GUARD") != "1":
            return None
        file_path = tool_input.get("file_path") or tool_input.get("path")
        if not file_path:
            return None
        # Imports tardios: só quem liga o Read guard paga o custo de carregar AST/SQLite
        from cato.code_intel.dedup import DEFAULT_SKELETON_LINE_THRESHOLD, evaluate_read_request
        from cato.storage.db import CatoDB

        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        threshold = int(os.environ.get("CATO_SKELETON_THRESHOLD", str(DEFAULT_SKELETON_LINE_THRESHOLD)))

        decision = evaluate_read_request(
            file_path=str(file_path),
            offset=int(offset) if offset is not None else None,
            limit=int(limit) if limit is not None else None,
            session_id=session_id,
            skeleton_threshold=threshold,
            db=db or CatoDB(),
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
            # Sem `permissionDecision`: a leitura segue pelo fluxo normal de permissões
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": decision.additional_context,
                }
            }
        return None

    # =========================================================================
    # 2. `Bash`: delegação ao RTK quando o hook global dele não está instalado
    # =========================================================================
    if tool_name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        if not command or rtk_hook_installed(payload.get("cwd")):
            return None
        # O Claude Code avalia as regras de permissão contra o comando reescrito; só reescrevemos
        # comandos que as regras do usuário já aprovam, senão o comando real escaparia de `deny`/`ask`.
        if not is_preapproved_bash_command(command, payload):
            return None
        rtk_result = try_delegate_to_rtk(payload)
        if rtk_result is not None:
            rtk_result.setdefault("hookSpecificOutput", {})["permissionDecision"] = "allow"
            return rtk_result
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
