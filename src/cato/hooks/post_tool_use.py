"""
Handler oficial do evento `PostToolUse` do Claude Code (Partes 4 e 5 do CATO).
Responsabilidades:
1. Verificador Sintático Instantâneo Zero-Turn após `Edit` / `Write` / `MultiEdit`
   (injeta erro exato via `additionalContext` no mesmo turno, poupando um turno inteiro de Bash).
2. Compressão da saída real de `Bash`/`PowerShell` via `updatedToolOutput` (sem reescrever o comando,
   portanto sem afetar permissões), só para o que o RTK não tratou.
3. Virtualizador SQLite FTS5 (BM25) para respostas gigantes de ferramentas MCP (`mcp__.*`),
   substituindo o payload bruto via `updatedMCPToolOutput` quando excede 1.500 tokens.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cato.bash_optimizer.parsers import ERROR_SIGNAL_RE
from cato.hooks.bash_permissions import is_rtk_command
from cato.storage.db import CatoDB, estimate_tokens

MCP_VIRTUALIZATION_TOKEN_THRESHOLD = 1500

# Comandos cuja saída é código/conteúdo que o modelo pediu para ler: nunca comprimir
CODE_READ_COMMANDS = {
    "sed", "cat", "head", "tail", "less", "more", "nl", "awk", "grep", "egrep", "rg", "bat", "jq",
    "get-content", "gc", "type", "select-string",
}


def _first_command_word(command: str) -> str:
    cmd = re.sub(r"^(cd\s+[^&;]+(&&|;)\s*)+", "", command.strip())
    first = cmd.split(None, 1)[0] if cmd else ""
    return Path(first.strip("'\"").replace("\\", "/")).name.lower()


def compress_shell_output(
    tool_name: str,
    tool_input: Dict[str, Any],
    tool_response: Any,
    session_id: str,
    db: Optional[CatoDB] = None,
) -> Optional[Dict[str, Any]]:
    """
    Comprime a saída REAL de `Bash`/`PowerShell` via `updatedToolOutput`, no mesmo formato
    (`{stdout, stderr, interrupted, isImage, ...}`). Não toca em: comandos já tratados pelo RTK,
    leitura de código, saídas interrompidas/imagem, nem em erros (stderr com sinal de erro).
    """
    if os.environ.get("CATO_BASH_COMPRESS") == "0" or not isinstance(tool_response, dict):
        return None
    stdout = tool_response.get("stdout")
    command = str(tool_input.get("command", ""))
    if not isinstance(stdout, str) or tool_response.get("interrupted") or tool_response.get("isImage"):
        return None
    if is_rtk_command(command) or _first_command_word(command) in CODE_READ_COMMANDS:
        return None
    stderr = str(tool_response.get("stderr") or "")
    if stderr.strip() and ERROR_SIGNAL_RE.search(stderr):
        return None
    from cato.bash_optimizer.runner import compress_and_record  # import tardio: só quando há saída a avaliar

    final_text = compress_and_record(command, stdout, session_id, db, tool_name=tool_name)
    if final_text is None:
        return None
    updated = dict(tool_response)
    updated["stdout"] = final_text
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}


def verify_file_syntax(file_path: str) -> Optional[str]:
    """
    Verifica instantaneamente (<10ms) se uma edição introduziu erro de sintaxe em arquivos Python ou JSON.
    Retorna a mensagem de diagnóstico se houver erro, ou `None` se estiver íntegro.
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return None

    ext = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    if ext == ".py":
        try:
            ast.parse(content, filename=str(path))
            return None
        except SyntaxError as exc:
            return (
                f"[CATO Zero-Turn Syntax Check] ⚠️ A edição em `{path.name}` introduziu um "
                f"SyntaxError na linha {exc.lineno}, coluna {exc.offset}: {exc.msg}. "
                f"Corrija com `Edit` imediatamente sem precisar rodar Bash para descobrir o erro."
            )

    if ext == ".json":
        try:
            json.loads(content)
            return None
        except json.JSONDecodeError as exc:
            return (
                f"[CATO Zero-Turn Syntax Check] ⚠️ O arquivo JSON `{path.name}` ficou inválido "
                f"na linha {exc.lineno}, coluna {exc.colno}: {exc.msg}."
            )

    return None


# Ferramentas MCP visuais/interativas que retornam screenshots Base64 ou estado imediato de UI
# e NUNCA devem ser virtualizadas em texto FTS5:
SKIP_MCP_PREFIXES = (
    "mcp__cato__",
    "mcp__claude_browser__",
    "mcp__playwright__",
    "mcp__puppeteer__",
    "mcp__chrome__",
    "mcp__browser__",
    "mcp__computer__",
    "mcp__claude-in-chrome__",
    "mcp__computer-use__",
)


def _extract_mcp_text(tool_response: Any) -> str:
    """
    Extrai apenas texto puro de uma resposta de ferramenta MCP.
    Se a resposta contiver imagens (`type == 'image'` ou dados base64), retorna string vazia
    para nunca corromper screenshots nem inflar contagem de tokens.
    """
    if isinstance(tool_response, str):
        if "data:image/" in tool_response or '"type":"image"' in tool_response or '"type": "image"' in tool_response:
            return ""
        return tool_response
    if isinstance(tool_response, dict):
        content = tool_response.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("image", "audio", "resource"):
                    return ""
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(parts) if parts else ""
        # Só serializa dicts puros que não contenham blobs binários/base64
        raw_json = json.dumps(tool_response, ensure_ascii=False, indent=2)
        if '"base64"' in raw_json or "iVBORw0KGgo" in raw_json:
            return ""
        return raw_json
    if isinstance(tool_response, list):
        for item in tool_response:
            if isinstance(item, dict) and item.get("type") in ("image", "audio"):
                return ""
        parts = [
            str(item.get("text", ""))
            for item in tool_response
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def handle_post_tool_use(
    payload: Dict[str, Any],
    db: Optional[CatoDB] = None,
) -> Optional[Dict[str, Any]]:
    """
    Processa o evento `PostToolUse` do Claude Code.
    """
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response")
    session_id = str(payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "default"))

    # =========================================================================
    # 1. VERIFICAÇÃO SINTÁTICA ZERO-TURN PÓS-EDIÇÃO (`Edit`, `Write`, `MultiEdit`)
    # =========================================================================
    if tool_name in ("Edit", "Write", "MultiEdit"):
        file_path = tool_input.get("file_path") or tool_input.get("path")
        if file_path:
            diag = verify_file_syntax(str(file_path))
            if diag:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": diag,
                    }
                }
        return None

    # =========================================================================
    # 2. COMPRESSÃO DA SAÍDA REAL DE `Bash` / `PowerShell` (o que o RTK não tratou)
    # =========================================================================
    if tool_name in ("Bash", "PowerShell"):
        # O banco só é aberto se houver o que comprimir (cada hook é um processo novo)
        return compress_shell_output(tool_name, tool_input, tool_response, session_id, db)

    # =========================================================================
    # 3. VIRTUALIZAÇÃO SQLITE FTS5 PARA FERRAMENTAS MCP DE TEXTO (`mcp__.*`)
    #    Ignora ferramentas de navegador/screenshots (ex: `mcp__Claude_Browser__*`)
    # =========================================================================
    tool_lower = tool_name.lower()
    if tool_lower.startswith("mcp__") and not any(tool_lower.startswith(p) for p in SKIP_MCP_PREFIXES):
        raw_text = _extract_mcp_text(tool_response)
        raw_tokens = estimate_tokens(raw_text)

        if raw_tokens >= MCP_VIRTUALIZATION_TOKEN_THRESHOLD:
            lines = raw_text.splitlines()
            chunk_size = 20
            chunks: List[Dict[str, str]] = []
            for idx in range(0, len(lines), chunk_size):
                slice_lines = lines[idx : idx + chunk_size]
                title = f"Linhas {idx + 1}-{min(len(lines), idx + chunk_size)}"
                chunks.append({"title": title, "content": "\n".join(slice_lines)})

            sbx_hash = hashlib.sha256(f"{time.time()}:{tool_name}".encode("utf-8")).hexdigest()[:8]
            sandbox_id = f"sbx_{sbx_hash}"

            preview_lines = "\n".join(ln[:160] for ln in lines[:15])
            if len(preview_lines) > 1200:
                preview_lines = preview_lines[:1200] + "..."
            summary_output = (
                f"[CATO MCP FTS5 Sandbox #{sandbox_id} — {raw_tokens} tokens virtualizados em {len(chunks)} blocos]\n"
                f"A resposta de `{tool_name}` ({len(lines)} linhas) foi indexada no banco local SQLite FTS5 (BM25) "
                f"para proteger sua janela de contexto.\n"
                f"👉 Para buscar termos específicos neste payload, use a ferramenta MCP `cato_search_sandbox` "
                f"com `id=\"{sandbox_id}\"` ou execute `cato search-sandbox {sandbox_id} \"<termo>\"`.\n\n"
                f"--- Prévia (Primeiras 15 linhas) ---\n{preview_lines}\n"
                f"... [+{max(0, len(lines) - 15)} linhas indexadas em #{sandbox_id}]"
            )
            opt_tokens = estimate_tokens(summary_output)

            db_instance = db or CatoDB()
            db_instance.store_sandbox_payload(
                sandbox_id=sandbox_id,
                session_id=session_id,
                source_tool=tool_name,
                chunks=chunks,
                raw_tokens=raw_tokens,
                summary_tokens=opt_tokens,
            )
            db_instance.record_telemetry(
                session_id=session_id,
                event_type="mcp_fts5_sandbox",
                tool_name=tool_name,
                target=f"{tool_name} (#{sandbox_id})",
                raw_tokens=raw_tokens,
                optimized_tokens=opt_tokens,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedMCPToolOutput": summary_output,
                }
            }

    return None


def main_stdio() -> int:
    """Lê o evento `PostToolUse` do stdin e imprime a resposta JSON no stdout."""
    try:
        raw_stdin = sys.stdin.read()
        if not raw_stdin.strip():
            return 0
        payload = json.loads(raw_stdin)
        response = handle_post_tool_use(payload)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception:
        return 0
    return 0
