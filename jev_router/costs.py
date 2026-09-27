"""Notional standard-rate costs for supported native assistant models.

Rates are USD per million tokens, snapshotted from the published catalogs on
2026-09-27. They are comparison estimates, not subscription charges.
Claude: https://platform.claude.com/docs/en/about-claude/pricing
OpenAI: https://developers.openai.com/api/docs/pricing
"""

from __future__ import annotations

from typing import Any


# uncached input, cache read, 5-minute cache write, output, tokenizer factor
RATES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-haiku-4-5-20251001": (1, 0.1, 1.25, 5, 1),
    "claude-sonnet-5": (2, 0.2, 2.5, 10, 1.3),
    "claude-opus-5-5": (4, 0.2, 5, 20, 1.3),
    "gpt-6-luna": (0.1, 0.01, 0.125, 0.5, 1),
    "gpt-6-sol": (2, 0.2, 2.5, 10, 1),
    "gpt-6-astra": (10, 1, 12.5, 50, 1),
}
ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5-5",
}


def known_model(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    model = ALIASES.get(value, value)
    return model if model in RATES else None


def estimate_usd(parts: list[dict[str, Any]], model: str, observed_model: str) -> float | None:
    """Price observed usage as if the same text ran on ``model``."""
    rates = RATES.get(model)
    observed = RATES.get(observed_model)
    if rates is None or observed is None:
        return None
    input_rate, read_rate, write_rate, output_rate, tokenizer = rates
    scale = tokenizer / observed[4]
    total = 0.0
    for part in parts:
        cached = part["cached_input_tokens"]
        write = part["cache_write_tokens"]
        if write is None:
            if model.startswith("claude-") and cached:
                return None  # Old Claude records did not separate cache reads and writes.
            write = 0
        read = cached - write
        uncached = part["input_tokens"] - cached
        if min(read, uncached, write) < 0:
            return None
        total += scale * (uncached * input_rate + read * read_rate + write * write_rate
                          + part["output_tokens"] * output_rate) / 1_000_000
    return round(total, 8)
