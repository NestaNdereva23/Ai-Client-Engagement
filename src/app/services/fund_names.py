from __future__ import annotations


def short_fund_name(fund_name: str) -> str:
    words = fund_name.split()
    if len(words) < 2 or any(word.isdigit() for word in words):
        return fund_name
    return "".join(word[0].upper() for word in words)
