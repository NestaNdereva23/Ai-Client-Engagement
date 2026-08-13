"""A static guard for AM12.2: dust_cleanup is an ops-only route with no
send capability anywhere near it. Nothing in campaigns/ -- eligibility,
enrollment, bucketing, or anywhere else -- may reference it at all; the
digest builder is restricted to DIGEST_ROUTES for the same reason.
"""

from __future__ import annotations

from pathlib import Path

from app.digest.build import DIGEST_ROUTES

CAMPAIGNS_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "campaigns"


def test_no_campaigns_module_references_dust_cleanup() -> None:
    offenders = [
        path
        for path in CAMPAIGNS_DIR.glob("*.py")
        if "dust_cleanup" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"campaigns/ must never reference dust_cleanup: {offenders}"


def test_digest_never_shows_dust_cleanup() -> None:
    assert "dust_cleanup" not in DIGEST_ROUTES
