"""
Tabela de preços da API da Anthropic (USD por milhão de tokens) usada para converter o `usage`
real dos transcripts do Claude Code em custo equivalente de API.

Fonte: https://platform.claude.com/docs/en/about-claude/pricing (out/2026).
Sobrescrevível por `~/.cato/pricing.json` no formato
`{"claude-opus-5-5": {"input": 4, "write_5m": 5, "write_1h": 8, "read": 0.2, "output": 20}}`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from cato.storage.db import get_cato_home


@dataclass(frozen=True)
class ModelPrice:
    input: float
    write_5m: float
    write_1h: float
    read: float
    output: float


def _legacy(input_price: float, output_price: float) -> ModelPrice:
    """Modelos 4.x: escrita 1,25x (5 min) / 2x (1 h), leitura 0,1x."""
    return ModelPrice(input_price, input_price * 1.25, input_price * 2, input_price * 0.1, output_price)


# Prefixo do model id -> preço. A busca usa o prefixo mais longo que casar.
DEFAULT_PRICES: Dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-sonnet-5": ModelPrice(2.0, 2.5, 4.0, 0.10, 10.0),
    "claude-haiku-5": ModelPrice(0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-fable-5": ModelPrice(4.0, 5.0, 8.0, 0.10, 20.0),
    "claude-opus-4": _legacy(5.0, 25.0),
    "claude-sonnet-4": _legacy(3.0, 15.0),
    "claude-haiku-4": _legacy(1.0, 5.0),
}

FALLBACK_PRICE = DEFAULT_PRICES["claude-opus-5"]

_overrides: Optional[Dict[str, ModelPrice]] = None


def _load_overrides() -> Dict[str, ModelPrice]:
    global _overrides
    if _overrides is None:
        _overrides = {}
        try:
            raw: Dict[str, Any] = json.loads((get_cato_home() / "pricing.json").read_text(encoding="utf-8"))
            for model, p in raw.items():
                _overrides[model] = ModelPrice(
                    float(p["input"]), float(p["write_5m"]), float(p["write_1h"]), float(p["read"]), float(p["output"])
                )
        except Exception:
            pass
    return _overrides


def price_for(model: str) -> ModelPrice:
    """Preço do modelo pelo prefixo mais longo (overrides do usuário têm prioridade)."""
    model = (model or "").lower()
    for table in (_load_overrides(), DEFAULT_PRICES):
        matches = [key for key in table if model.startswith(key)]
        if matches:
            return table[max(matches, key=len)]
    return FALLBACK_PRICE


def usage_cost(model: str, usage: Dict[str, Any]) -> Dict[str, float]:
    """Converte um bloco `usage` da API em custo (USD) por categoria."""
    p = price_for(model)
    creation = usage.get("cache_creation") or {}
    w5 = creation.get("ephemeral_5m_input_tokens")
    w1 = creation.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = usage.get("cache_creation_input_tokens") or 0, 0
    return {
        "input": (usage.get("input_tokens") or 0) * p.input / 1e6,
        "cache_write_5m": (w5 or 0) * p.write_5m / 1e6,
        "cache_write_1h": (w1 or 0) * p.write_1h / 1e6,
        "cache_read": (usage.get("cache_read_input_tokens") or 0) * p.read / 1e6,
        "output": (usage.get("output_tokens") or 0) * p.output / 1e6,
    }
