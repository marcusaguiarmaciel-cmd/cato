"""
Handler do evento `UserPromptSubmit` do Claude Code — Guard de custo de contexto.

Nos transcripts reais, o custo é dominado por releitura de contexto grande a cada turno e por
reescritas de cache depois que o TTL expira. Antes do prompt ir para a API, este guard lê o fim
do transcript e, uma única vez por situação, segura o prompt mostrando o custo concreto:
1. Cache expirado (ocioso além do TTL) com contexto grande: continuar reescreve o contexto inteiro;
2. Contexto acima de cada faixa (`CATO_CONTEXT_WARN`, padrão 250k/400k/600k): custo por turno.
Reenviar o prompt segue normalmente (o aviso fica registrado em `guard_acks`). Comandos `/...`
nunca são bloqueados. `CATO_GUARD=0` desliga. Qualquer erro deixa o prompt passar (fail-open).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from cato.storage.db import CatoDB
from cato.telemetry.pricing import price_for
from cato.telemetry.usage_report import last_turn_state

IDLE_MIN_CONTEXT = 150_000
DEFAULT_CONTEXT_BANDS = (250_000, 400_000, 600_000)
# Tamanho típico do resumo gerado pelo /compact (tokens de saída)
COMPACT_SUMMARY_TOKENS = 15_000


def _context_bands() -> List[int]:
    raw = os.environ.get("CATO_CONTEXT_WARN", "")
    try:
        bands = sorted(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        bands = []
    return bands or list(DEFAULT_CONTEXT_BANDS)


def _block(reason: str) -> Dict[str, Any]:
    return {"decision": "block", "reason": reason}


def handle_user_prompt_submit(
    payload: Dict[str, Any],
    db: Optional[CatoDB] = None,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if os.environ.get("CATO_GUARD") == "0":
        return None
    prompt = str(payload.get("prompt") or "").lstrip()
    if prompt.startswith(("/", "!")):
        return None
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    state = last_turn_state(str(transcript))
    if not state or not state.get("timestamp"):
        return None

    session_id = str(payload.get("session_id") or "default")
    db_instance = db or CatoDB()
    now = time.time() if now is None else now
    price = price_for(state["model"])
    ctx = int(state["context"])
    ttl = int(state["ttl_seconds"])
    idle = now - float(state["timestamp"])
    per_turn = ctx * price.read / 1e6
    compact_warm = per_turn + COMPACT_SUMMARY_TOKENS * price.output / 1e6

    # 1. Cache expirado: o próximo turno reescreve o contexto inteiro
    if idle > ttl and ctx >= IDLE_MIN_CONTEXT:
        ack_key = f"idle:{int(state['timestamp'])}"
        if db_instance.has_guard_ack(session_id, ack_key):
            return None
        db_instance.add_guard_ack(session_id, ack_key)
        write_price = price.write_1h if ttl >= 3600 else price.write_5m
        rewrite = ctx * write_price / 1e6
        compact_cold = ctx * price.write_5m / 1e6 + COMPACT_SUMMARY_TOKENS * price.output / 1e6
        return _block(
            f"[CATO] O cache desta conversa expirou ({int(idle // 60)} min parado; o cache dura {ttl // 60} min). "
            f"Continuar agora reescreve {ctx:,} tokens de contexto ≈ ${rewrite:.2f}, e cada turno seguinte relê "
            f"esse contexto (≈ ${per_turn:.3f}/turno).\n"
            f"• /clear ≈ $0 — recomeça limpo (melhor se a próxima tarefa é outra)\n"
            f"• /compact ≈ ${compact_cold:.2f} uma vez — resume e o contexto cai para ~{COMPACT_SUMMARY_TOKENS // 1000}k\n"
            f"• Reenvie o mesmo prompt para continuar assim mesmo."
        )

    # 2. Contexto grande: cada turno relê tudo
    crossed = [band for band in _context_bands() if ctx >= band]
    if crossed:
        band = crossed[-1]
        ack_key = f"ctx:{band}"
        if db_instance.has_guard_ack(session_id, ack_key):
            return None
        db_instance.add_guard_ack(session_id, ack_key)
        return _block(
            f"[CATO] O contexto está em {ctx // 1000}k tokens. Cada turno relê tudo: ≈ ${per_turn:.3f}/turno "
            f"(≈ ${per_turn * 100:.2f} a cada 100 turnos), e se a conversa ficar parada mais de {ttl // 60} min "
            f"a reescrita custa ≈ ${ctx * (price.write_1h if ttl >= 3600 else price.write_5m) / 1e6:.2f}.\n"
            f"• /compact ≈ ${compact_warm:.2f} uma vez — o contexto cai para ~{COMPACT_SUMMARY_TOKENS // 1000}k\n"
            f"• /clear se a próxima tarefa é outra\n"
            f"• Reenvie o mesmo prompt para continuar assim mesmo (próximo aviso só na faixa seguinte)."
        )
    return None


def main_stdio() -> int:
    """Lê o evento `UserPromptSubmit` do stdin e imprime a decisão JSON no stdout."""
    try:
        raw_stdin = sys.stdin.read()
        if not raw_stdin.strip():
            return 0
        response = handle_user_prompt_submit(json.loads(raw_stdin))
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    except Exception:
        return 0
    return 0
