from __future__ import annotations

import pytest

from app.agents import card_copy
from app.agents.watchlist import GROUP_NAMES


@pytest.mark.parametrize("group_name", GROUP_NAMES)
def test_every_current_group_has_card_copy(group_name) -> None:
    copy = card_copy.copy_for(group_name)

    assert copy.screen_label and copy.card_title and copy.why_now


def test_a_group_that_is_no_longer_produced_has_no_current_copy() -> None:
    with pytest.raises(card_copy.UnknownSituation):
        card_copy.copy_for("healthy_one_fund")


def test_a_stored_proposal_from_an_old_group_still_reads_plainly() -> None:
    copy = card_copy.copy_for_history("healthy_one_fund")

    assert copy.screen_label == "Healthy one fund"
    assert copy.why_now == "Earlier suggestion"
