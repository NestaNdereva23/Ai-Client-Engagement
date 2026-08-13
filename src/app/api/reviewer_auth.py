"""Interim protection for the endpoints that re-attach a client's real name,
render a briefing, or record a review decision.

Nothing in this codebase authenticates a human reviewer with a real login
yet -- no session, no role. The safe default, until real auth exists, is a
small hardcoded list of reviewers, each with their own static key, set in
REVIEWERS (see app.config.Settings.reviewers): "alice:key1,bob:key2". A
caller sends their key in X-Reviewer-Key; this resolves it to the
reviewer_id that key belongs to, so an audited action names the reviewer
who actually made the call, not whatever string a request body claims.
With no reviewers configured, every endpoint behind this refuses every
request rather than run unprotected.

This is a minimum acceptable gate, not the actual decision. Do not set
REVIEWERS in any environment holding real client data until real
session/role auth exists.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import get_settings

REVIEWER_KEY_HEADER = "X-Reviewer-Key"


def get_current_reviewer_id(x_reviewer_key: str | None = Header(default=None)) -> str:
    """Resolve X-Reviewer-Key to the reviewer_id it belongs to.

    503 with no reviewers configured, 401 for a missing or unrecognized
    key. Checks every configured key rather than stopping at the first
    match, so response timing doesn't hint at which keys exist.
    """
    reviewer_keys = get_settings().reviewer_keys
    if not reviewer_keys:
        raise HTTPException(status_code=503, detail="no reviewers are configured")
    if not x_reviewer_key:
        raise HTTPException(status_code=401, detail="invalid or missing reviewer key")

    matched: str | None = None
    for key, reviewer_id in reviewer_keys.items():
        if secrets.compare_digest(x_reviewer_key, key):
            matched = reviewer_id
    if matched is None:
        raise HTTPException(status_code=401, detail="invalid or missing reviewer key")
    return matched
