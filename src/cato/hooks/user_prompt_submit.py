"""
Handler do evento `UserPromptSubmit` do Claude Code — Guard de custo de contexto.

Nos transcripts reais, o custo é dominado por releitura de contexto grande a cada turno e por
reescritas de cache depois que o TTL expira. Antes do prompt ir para a API, este guard lê o fim
do transcript e:
1. Cache expirado (ocioso além do TTL) com contexto grande: se reescrever o contexto custa pelo menos
   `CATO_GUARD_BLOCK_USD` (padrão $1,00), segura o prompt UMA vez mostrando o custo; reenviar segue.
   Abaixo disso, ou com `CATO_GUARD_BLOCK=0`, só avisa e o prompt segue.
2. Contexto acima de cada faixa (`CATO_CONTEXT_WARN`, padrão 250k/400k/600k): só avisa, uma vez por faixa.
Avisos usam `systemMessage` (aparecem para você, não entram no contexto do modelo). Comandos `/...` e
`!...` nunca são tocados; depois de um `/compact` o guard espera a próxima chamada real antes de medir
de novo. `CATO_GUARD=0` desliga tudo. Qualquer erro deixa o prompt passar (fail-open).
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
DEFAULT_BLOCK_USD = 1.0
# Tamanho típico do resumo gerado pelo /compact (tokens de saída)
COMPACT_SUMMARY_TOKENS = 15_000


def _context_bands() -> List[int]:
    raw = os.environ.get("CATO_CONTEXT_WARN", "")
    try:
        bands = sorted(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        bands = []
    return bands or list(DEFAULT_CONTEXT_BANDS)


def _block_threshold_usd() -> Optional[float]:
    """Custo mínimo de reescrita para segurar o prompt; `None` quando bloquear está desligado."""
    if os.environ.get("CATO_GUARD_BLOCK") == "0":
        return None
    try:
        return float(os.environ.get("CATO_GUARD_BLOCK_USD") or DEFAULT_BLOCK_USD)
    except ValueError:
        return DEFAULT_BLOCK_USD


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
    # Depois de um /compact o último `usage` é o do contexto antigo: espera a próxima chamada real
    if not state or not state.get("timestamp") or state.get("compacted"):
        return None

    session_id = str(payload.get("session_id") or "default")
    db_instance = db or CatoDB()
    now = time.time() if now is None else now
    price = price_for(state["model"])
    ctx = int(state["context"])
    ttl = int(state["ttl_seconds"])
    idle = now - float(state["timestamp"])
    per_turn = ctx * price.read / 1e6
    write_price = price.write_1h if ttl >= 3600 else price.write_5m

    # 1. Cache expirado: o próximo turno reescreve o contexto inteiro
    if idle > ttl and ctx >= IDLE_MIN_CONTEXT:
        ack_key = f"idle:{int(state['timestamp'])}"
        if db_instance.has_guard_ack(session_id, ack_key):
            return None
        db_instance.add_guard_ack(session_id, ack_key)
        rewrite = ctx * write_price / 1e6
        compact_cold = ctx * price.write_5m / 1e6 + COMPACT_SUMMARY_TOKENS * price.output / 1e6
        options = (
            f"• /clear ≈ $0 — recomeça limpo (melhor se a próxima tarefa é outra)\n"
            f"• /compact ≈ ${compact_cold:.2f} uma vez — resume e o contexto cai para ~{COMPACT_SUMMARY_TOKENS // 1000}k"
        )
        threshold = _block_threshold_usd()
        if threshold is not None and rewrite >= threshold:
            return {
                "decision": "block",
                "reason": (
                    f"[CATO] Prompt segurado uma vez para evitar gasto: o cache desta conversa expirou "
                    f"({int(idle // 60)} min parada; o cache dura {ttl // 60} min) e continuar agora reescreve "
                    f"{ctx:,} tokens ≈ ${rewrite:.2f}.\n{options}\n"
                    f"• Para continuar assim mesmo, envie o prompt de novo (não será segurado outra vez).\n"
                    f"Ajustes: CATO_GUARD_BLOCK_USD (padrão $1.00) ou CATO_GUARD_BLOCK=0 para só avisar."
                ),
            }
        return {
            "systemMessage": (
                f"[CATO] O cache expirou ({int(idle // 60)} min parada): este turno reescreve {ctx:,} tokens "
                f"≈ ${rewrite:.2f}.\n{options}"
            )
        }

    # 2. Contexto grande: só avisa (cada turno relê tudo)
    crossed = [band for band in _context_bands() if ctx >= band]
    if crossed:
        band = crossed[-1]
        ack_key = f"ctx:{band}"
        if db_instance.has_guard_ack(session_id, ack_key):
            return None
        db_instance.add_guard_ack(session_id, ack_key)
        compact_warm = per_turn + COMPACT_SUMMARY_TOKENS * price.output / 1e6
        return {
            "systemMessage": (
                f"[CATO] Contexto em {ctx // 1000}k tokens: cada turno relê tudo (≈ ${per_turn:.3f}/turno, "
                f"≈ ${per_turn * 100:.2f} a cada 100 turnos). /compact ≈ ${compact_warm:.2f} uma vez reduz para "
                f"~{COMPACT_SUMMARY_TOKENS // 1000}k; /clear se a próxima tarefa é outra."
            )
        }
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
