"""
Ponto de entrada mínimo dos hooks do Claude Code: `python hook_runner.py <evento>`.

Cada hook roda num processo novo, então o tempo de import pesa em toda chamada de ferramenta. Este
módulo importa só o handler do evento pedido (o `cli.py` carrega dashboard, servidor HTTP, MCP etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


def run(event: str) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    if event == "pre-tool-use":
        from cato.hooks.pre_tool_use import main_stdio

        return main_stdio()
    if event == "post-tool-use":
        from cato.hooks.post_tool_use import main_stdio

        return main_stdio()
    if event == "user-prompt-submit":
        from cato.hooks.user_prompt_submit import main_stdio

        return main_stdio()
    if event in ("pre-compact", "session-reset"):
        from cato.hooks.session_lifecycle import main_stdio

        return main_stdio(mode=event)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else ""))
