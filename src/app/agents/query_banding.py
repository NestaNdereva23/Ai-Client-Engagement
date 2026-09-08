"""How a query answer is rounded off before the model sees it.

Two rules. Money is rounded, never exact, so no figure can be matched back
to one account. A group too small to hide in is not reported at all: with a
narrow enough filter a count of one is a person, so anything under the
minimum comes back withheld with a plain reason instead of a number.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings

TOO_SMALL_NOTE = "fewer clients than this agent is allowed to report on"


def round_money(amount: float | None) -> float | None:
    """Money, rounded coarsely enough that it is a size and not a balance."""
    if amount is None:
        return None
    size = abs(amount)
    if size < 10_000:
        step = 1_000
    elif size < 1_000_000:
        step = 10_000
    elif size < 100_000_000:
        step = 100_000
    else:
        step = 1_000_000
    return float(round(amount / step) * step)


def is_too_small(client_count: int) -> bool:
    """True when a group has members but too few to report on."""
    return 0 < client_count < get_settings().agent_query_min_group_size


def band_measures(measures: dict[str, Any], client_count: int) -> dict[str, Any]:
    """One set of measures, rounded and withheld where it has to be."""
    if is_too_small(client_count):
        return {"too_small": True, "note": TOO_SMALL_NOTE}
    banded: dict[str, Any] = {"too_small": False}
    for name, value in measures.items():
        if name.endswith("_kes"):
            banded[name] = round_money(value)
        elif name == "avg_risk_score":
            banded[name] = None if value is None else int(round(value))
        else:
            banded[name] = value
    return banded


def band_label(bands: tuple[float, ...], value: float | None) -> str:
    """Which band one number falls in, as the label the model reads."""
    if value is None:
        return "unknown"
    if not bands:
        return str(value)
    if value < bands[0]:
        return f"under {_number(bands[0])}"
    for low, high in zip(bands, bands[1:], strict=False):
        if value < high:
            return f"{_number(low)} to {_number(high)}"
    return f"{_number(bands[-1])} and above"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
