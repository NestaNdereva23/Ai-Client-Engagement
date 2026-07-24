"""The privacy boundary is the single, fail-closed path to the model.

These prove the payload is projected to the allow-list, both scanners run around
the call, a hit aborts without returning a draft, and no module outside
app.privacy imports the model SDK directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.privacy.boundary as boundary
from app.privacy.boundary import run_model_boundary, to_model_context
from app.privacy.scanners import InboundLeak, OutboundLeak, scan_inbound

ALLOWLISTED = {
    "archetype": "One-and-done",
    "recency_bucket": "Exited 3y plus",
    "value_tier_label": "High",
    "rhythm_band": "Unknown",
}


def test_to_model_context_keeps_only_allowlisted_keys() -> None:
    row = {**ALLOWLISTED, "client_id": 1001, "own_rhythm_days": 42, "client_name": "Jane"}
    assert to_model_context(row) == ALLOWLISTED


def test_run_model_boundary_sends_only_the_allowlisted_payload() -> None:
    seen: dict = {}

    def model_call(payload: dict) -> str:
        seen.update(payload)
        return "Hi {{first_name}}"

    draft = run_model_boundary(ALLOWLISTED, model_call)
    assert draft == "Hi {{first_name}}"
    assert seen == ALLOWLISTED


def test_inbound_hit_aborts_before_the_model_is_called() -> None:
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    # client_id is a re-attachment key, not allow-listed, so it must never be sent.
    with pytest.raises(InboundLeak):
        run_model_boundary({**ALLOWLISTED, "client_id": 1001}, model_call)
    assert called is False


def test_outbound_hit_aborts_after_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocking_scan(draft: str) -> None:
        raise OutboundLeak("echoed an identifier")

    monkeypatch.setattr(boundary, "scan_outbound", blocking_scan)
    with pytest.raises(OutboundLeak):
        run_model_boundary(ALLOWLISTED, lambda payload: "leaky draft")


def test_scan_inbound_rejects_offlist_keys() -> None:
    with pytest.raises(InboundLeak, match="allow-list"):
        scan_inbound({"client_name": "Jane"})


def test_scan_inbound_passes_allowlisted_only() -> None:
    assert scan_inbound(ALLOWLISTED) is None


def test_only_privacy_imports_the_model_sdk() -> None:
    """No module outside app.privacy may import the model SDK directly."""
    app_root = Path(boundary.__file__).parents[1]
    privacy_dir = app_root / "privacy"
    sdk_import = re.compile(r"^\s*(import|from)\s+anthropic\b", re.MULTILINE)

    offenders = [
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if privacy_dir not in path.parents and sdk_import.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
