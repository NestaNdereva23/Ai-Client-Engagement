from __future__ import annotations

from app.risk.routing import ROUTE_FA_ACTION, ROUTES, route_direction, route_for_active_client

# --- route_direction ---


def test_route_direction_is_none_for_a_first_ever_scored_run() -> None:
    assert route_direction(None, "fa_watchlist") is None


def test_route_direction_more_urgent_when_to_route_outranks_from_route() -> None:
    assert route_direction("monitor_only", "fa_call_priority") == "more_urgent"


def test_route_direction_less_urgent_when_to_route_ranks_lower() -> None:
    assert route_direction("fa_call_priority", "auto_checkin") == "less_urgent"


def test_route_direction_unchanged_for_a_same_route_move() -> None:
    assert route_direction("fa_watchlist", "fa_watchlist") == "unchanged"


def test_route_direction_resolves_retired_route_names_from_old_history() -> None:
    """risk_snapshot/audit_log rows written before the terminology rename
    (migration ece296116d34) still spell routes the old way and are never
    rewritten -- route_direction has to rank those without raising.
    """
    assert route_direction("fa_digest_watch", "fa_call_priority") == "more_urgent"
    assert route_direction("dust_cleanup", "automated_nurture") == "more_urgent"
    assert route_direction("automated_nurture", "automated_nurture") == "unchanged"


# --- fa_action ---


def test_every_active_client_goes_to_fa_action_with_no_queue_rank() -> None:
    result = route_for_active_client(has_open_complaint=False)
    assert result.route == ROUTE_FA_ACTION
    assert result.queue_rank is None
    assert result.complaint_caveat is False


def test_an_open_complaint_is_carried_on_the_fa_action_result() -> None:
    assert route_for_active_client(has_open_complaint=True).complaint_caveat is True


def test_fa_action_is_a_known_route() -> None:
    assert ROUTE_FA_ACTION in ROUTES


def test_a_move_into_or_out_of_fa_action_is_never_labelled_an_escalation() -> None:
    assert route_direction("monitor_only", ROUTE_FA_ACTION) is None
    assert route_direction("fa_call_priority", ROUTE_FA_ACTION) is None
    assert route_direction(ROUTE_FA_ACTION, "fa_watchlist") is None
    assert route_direction(ROUTE_FA_ACTION, ROUTE_FA_ACTION) is None
