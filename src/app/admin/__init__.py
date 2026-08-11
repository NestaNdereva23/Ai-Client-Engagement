"""Read-only operational admin (SQLAdmin), for campaign and rules config.

Everything registered here is config a human legitimately inspects and, once
we trust the audit coverage, edits: campaigns, campaign steps, message
templates, business rules, the angle catalogue, and tier contracts. Nothing
holding real PII (pii_vault) or a client_id/client_code (outreach_message,
enrollment, touch_log, suppression, client_message_indicators) is registered
here, restricted per the anonymization boundary.
"""

from __future__ import annotations
