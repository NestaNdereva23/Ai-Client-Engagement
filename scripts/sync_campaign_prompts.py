from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.agents.campaign_prompt import DEFAULTS
from app.db.session import SessionLocal
from app.rules import versioning


def sync_campaign_prompts(by: str = "system") -> None:
    with SessionLocal() as session:
        for prompt_name in ("campaign_suggest", "campaign_plan"):
            template = DEFAULTS[prompt_name]
            version = versioning.save_draft(
                session,
                "campaign_agent_prompt",
                prompt_name,
                [{"template": template}],
                by=by,
            )
            versioning.publish(
                session,
                "campaign_agent_prompt",
                prompt_name,
                version,
                by=by,
            )
            print(f"Published updated {prompt_name} prompt as version {version}")
        session.commit()


if __name__ == "__main__":
    sync_campaign_prompts()
