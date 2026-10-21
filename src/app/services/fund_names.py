from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.models import Funds


def short_fund_name(fund_name: str) -> str:
    words = fund_name.split()
    if len(words) < 2 or any(word.isdigit() for word in words):
        return fund_name
    return "".join(word[0].upper() for word in words)


def fund_short_names(session: Session, fund_ids: set[int]) -> dict[int, str]:
    """unit_fund_id -> short code, for whichever of the given ids are known."""
    if not fund_ids:
        return {}
    rows = session.execute(
        select(Funds.unit_fund_id, Funds.unit_fund_name).where(Funds.unit_fund_id.in_(fund_ids))
    ).all()
    return {fund_id: short_fund_name(name) for fund_id, name in rows}
