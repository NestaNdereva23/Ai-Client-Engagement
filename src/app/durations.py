from __future__ import annotations

import re

_DURATION = re.compile(r"^\s*(\d+)\s*([hd])\s*$", re.IGNORECASE)
_HOURS_PER_UNIT = {"h": 1, "d": 24}


def parse_hours(text: str, *, setting: str) -> int:
    match = _DURATION.match(text)
    hours = int(match.group(1)) * _HOURS_PER_UNIT[match.group(2).lower()] if match else 0
    if hours <= 0:
        raise ValueError(f"{setting} must look like 12h or 7d, not {text!r}")
    return hours
