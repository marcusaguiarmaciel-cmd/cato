"""
Handler de ciclo de vida de sessão (`SessionStart` para `clear|compact` e `PreCompact` — Parte 5 do CATO).
Garante que:
1. Antes de `/compact` (`PreCompact`), o estado determinístico do Git (branch, arquivos modificados,
   diff stat e leituras recentes) seja salvo no SQLite;
2. O cache de deduplicação SHA-256 seja limpo para que o Claude possa reler arquivos após a compactação;
3. Logo após a compactação (`SessionStart` com `matcher: "compact"`), o resumo determinístico de estado
   seja reinjetado automaticamente para evitar *Trajectory Elongation* (repetição de comandos git).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict, Optional

from cato.storage.db import CatoDB


def _run_git_cmd(args: list[str], cwd: Optional[str] = None) -> str:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def handle_pre_compact(payload: Dict[str, Any], db: Optional[CatoDB] = None) -> Dict[str, Any]:
    """Captura o estado determinístico do repositório antes da compactação e limpa `session_reads`."""
    session_id = str(payload.get("session_id") or "default")
    cwd = payload.get("cwd")
    db_instance = db or CatoDB()

    branch = _run_git_cmd(["branch", "--show-current"], cwd=cwd) or "unknown"
    status = _run_git_cmd(["status", "--short"], cwd=cwd)
    diff_stat = _run_git_cmd(["diff", "--stat"], cwd=cwd)

    db_instance.save_compaction_snapshot(
        session_id=session_id,
        git_branch=branch,
        git_status=status[:800],
        git_diff_stat=diff_stat[:800],
        recent_files="",
    )
    cleared = db_instance.clear_session_reads(session_id)
    return {"saved_snapshot": True, "cleared_reads": cleared, "session_id": session_id}


def handle_session_reset(payload: Dict[str, Any], db: Optional[CatoDB] = None) -> Dict[str, Any]:
    """
    Executado em `SessionStart` (`clear|compact`).
    Limpa `session_reads` e, se a origem for `compact`, retorna o estado determinístico do Git.
    """
    session_id = str(payload.get("session_id") or "default")
    source = str(payload.get("source", ""))
    db_instance = db or CatoDB()
    cleared = db_instance.clear_session_reads(session_id)
    db_instance.clear_guard_acks(session_id)

    reinjected_context: Optional[str] = None
    if source == "compact":
        snap = db_instance.pop_compaction_snapshot(session_id)
        if snap:
            reinjected_context = (
                f"[CATO Post-Compaction State Recovery — Branch: {snap.get('git_branch')}]\n"
                f"Arquivos modificados no Git (não é necessário rodar `git status` novamente):\n"
                f"{snap.get('git_status') or '(working tree clean)'}\n"
                f"{snap.get('git_diff_stat') or ''}"
            ).strip()

    return {
        "cleared_reads": cleared,
        "session_id": session_id,
        "additionalContext": reinjected_context,
    }


def main_stdio(mode: str = "session-reset") -> int:
    """Ponto de entrada stdio para hooks `PreCompact` e `SessionStart` (`clear|compact`)."""
    try:
        raw_stdin = sys.stdin.read()
        payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
        if mode == "pre-compact" or payload.get("hook_event_name") == "PreCompact":
            handle_pre_compact(payload)
        else:
            res = handle_session_reset(payload)
            if res.get("additionalContext"):
                out = {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": res["additionalContext"],
                    }
                }
                sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    except Exception:
        return 0
    return 0
