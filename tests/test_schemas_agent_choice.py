"""The AgentChoice shape: one action, one angle, and a reason, for one group.

Catalogue-aware checks (is this group offered tonight, is this action code
real, does the angle match) are the choose_action tool's job and are covered
in test_agent_loop.py; this file only covers the shape itself.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.agent_choice import AgentChoice


def test_a_well_formed_choice_builds() -> None:
    choice = AgentChoice(
        group_name="fees_will_empty",
        action_code="fee_warning",
        angle="sitting_still",
        reason="their balance runs out soon",
    )
    assert choice.group_name == "fees_will_empty"
    assert choice.action_code == "fee_warning"
    assert choice.angle == "sitting_still"


def test_a_null_angle_is_allowed() -> None:
    choice = AgentChoice(
        group_name="fees_will_empty",
        action_code="do_nothing",
        angle=None,
        reason="nobody qualifies tonight",
    )
    assert choice.angle is None


def test_a_blank_reason_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentChoice(
            group_name="fees_will_empty",
            action_code="fee_warning",
            angle="sitting_still",
            reason="   ",
        )


def test_a_blank_group_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentChoice(group_name="  ", action_code="fee_warning", reason="why")


def test_an_unexpected_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        AgentChoice(
            group_name="fees_will_empty",
            action_code="fee_warning",
            reason="why",
            extra_field="not allowed",
        )
