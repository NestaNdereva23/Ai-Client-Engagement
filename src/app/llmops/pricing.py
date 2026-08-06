"""Static per-token USD pricing, for cost-per-message accounting.

Anthropic's published list prices, in dollars per token (list price divided
by one million). This is a maintained snapshot, not a live lookup: a price
change needs a code change here, and until then cost figures are quietly
stale rather than wrong in an obvious way. A model missing from the table
returns a null cost rather than a wrong one, so an unpriced or unrecognized
model doesn't silently corrupt an average with an implicit zero.
"""

from __future__ import annotations

# (input $/token, output $/token), keyed by the model id stored on
# model_versions. Both the dated snapshot id and its bare alias are listed,
# since either can end up in config depending on what settings.llm_model was
# set to at generation time.
_ANTHROPIC_PRICE_PER_TOKEN_USD: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00 / 1_000_000, 5.00 / 1_000_000),
    "claude-haiku-4-5": (1.00 / 1_000_000, 5.00 / 1_000_000),
    "claude-sonnet-5": (3.00 / 1_000_000, 15.00 / 1_000_000),
    "claude-opus-5": (5.00 / 1_000_000, 25.00 / 1_000_000),
}

_PRICE_TABLES: dict[str, dict[str, tuple[float, float]]] = {
    "anthropic": _ANTHROPIC_PRICE_PER_TOKEN_USD,
}


def estimate_cost_usd(
    provider: str,
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    """The dollar cost of one call, or None when the model has no listed price
    or either token count is missing.
    """
    table = _PRICE_TABLES.get(provider)
    if table is None or model_id not in table:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    input_price, output_price = table[model_id]
    return input_tokens * input_price + output_tokens * output_price
