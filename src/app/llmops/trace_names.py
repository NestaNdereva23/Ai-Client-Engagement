from __future__ import annotations

AGENT_CAMPAIGN_RUN = "Agent campaign enrollment and email drafting"
AGENT_RECHECK_CLIENTS = "Re-check clients before enrolling"
AGENT_ENROLL_CLIENTS = "Enroll clients into the campaign"
AGENT_DRAFT_EMAILS = "Draft emails into review queue"
AGENT_TELL_ACCOUNT_MANAGERS = "Tell account managers"

CAMPAIGN_EMAIL_DRAFT = "Campaign email draft"

ASK_CHAT = "Ask chat"

ASSISTANT_TURN_BY_INTENT = {
    "quick_email": "Client assistant: write a quick email",
    "email_options": "Client assistant: suggest email options",
    "draft_email": "Client assistant: write the chosen email",
    "deeper_brief": "Client assistant: deeper brief",
    "free": "Client assistant: open question",
}
ASSISTANT_TURN_DEFAULT = "Client assistant: turn"

CLIENT_EMAIL_DRAFT_TRACE = "Client email draft"
CLIENT_EMAIL_SAVE = "Save the draft to the review queue"
SUGGEST_EMAIL_OPTIONS = "Suggest email options"

MODEL_WRITE_CLIENT_EMAIL = "Model call: write client email"
MODEL_SUGGEST_EMAIL_OPTIONS = "Model call: suggest email options"
MODEL_GENERATE_ANGLE_BRIEF = "Model call: generate proposal angle brief"
MODEL_WRITE_CLIENT_BRIEF = "Model call: write client brief"
MODEL_WRITE_BOOK_BRIEF = "Model call: write book brief"

TAG_AGENT = "agent"
TAG_ASSISTANT = "assistant"
TAG_CAMPAIGN = "campaign"
TAG_CHAT = "chat"
TAG_EMAIL = "email"


def assistant_turn_name(intent: str | None) -> str:
    return ASSISTANT_TURN_BY_INTENT.get(intent or "free", ASSISTANT_TURN_DEFAULT)
