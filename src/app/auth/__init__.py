"""Real login for the reviewer console: password hashing and session auth.

Replaces app.api.reviewer_auth's X-Reviewer-Key stopgap for a human at a
browser. That stopgap stays as-is for programmatic callers of the JSON
review API.
"""
