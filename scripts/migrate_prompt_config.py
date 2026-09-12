from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.agents.email_agent import (  # noqa: E402
    _BASE_INSTRUCTIONS_CORE,
    BANNED_WORDS,
    CAMPAIGN_PROHIBITIONS,
)
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.personalization.eligibility import (  # noqa: E402
    FACT_FIELDS,
    PLACEHOLDER_FACT_FIELDS,
    PLACEHOLDER_FIELDS,
    save_fact_eligibility_rule,
)
from app.rules import versioning  # noqa: E402

_OUTPUT_SCHEMA_NOTE = (
    "OUTPUT FORMAT: Return ONLY one valid JSON object with exactly two fields: "
    '{"subject": "...", "body": "..."}. Do not return markdown, code fences, '
    "explanations, notes, or any text before or after the JSON object. The output "
    "must be raw JSON."
)

_FORMATTING_RESTRICTIONS = {
    "body_target_words": 75,
    "body_min_words": 50,
    "body_max_words": 125,
    "subject_min_words": 4,
    "subject_max_words_preferred": 7,
    "subject_max_words": 10,
    "no_em_dashes": True,
}


def voice_contract_rows() -> list[dict]:
    return [{"body_markdown": _BASE_INSTRUCTIONS_CORE, "rendered_text": _BASE_INSTRUCTIONS_CORE}]


def safety_policy_rows() -> list[dict]:
    return [
        {
            "banned_words": list(BANNED_WORDS),
            "banned_phrases": [],
            "campaign_prohibitions": list(CAMPAIGN_PROHIBITIONS),
        }
    ]


def output_policy_rows() -> list[dict]:
    return [
        {
            "output_schema_note": _OUTPUT_SCHEMA_NOTE,
            "placeholder_rules": {"fields": list(PLACEHOLDER_FACT_FIELDS)},
            "formatting_restrictions": dict(_FORMATTING_RESTRICTIONS),
        }
    ]


def personalization_eligibility_rows() -> list[dict]:
    return [
        {
            "fact_field": field,
            "exposure_mode": "placeholder" if field in PLACEHOLDER_FIELDS else "direct",
        }
        for field in FACT_FIELDS
    ]


def build_seed() -> dict[str, list[dict]]:
    return {
        "voice_contract": voice_contract_rows(),
        "safety_policy": safety_policy_rows(),
        "output_policy": output_policy_rows(),
        "personalization_policy": [{}],
    }


def apply_seed(by: str) -> dict[str, int]:
    versions: dict[str, int] = {}
    default_key = versioning.DEFAULT_COMPONENT_KEY
    with SessionLocal() as session:
        for component_type in ("voice_contract", "safety_policy", "output_policy"):
            values = build_seed()[component_type]
            version = versioning.save_draft(session, component_type, default_key, values, by=by)
            versions[component_type] = version

        personalization_version = versioning.save_draft(
            session, "personalization_policy", default_key, [{}], by=by
        )
        versions["personalization_policy"] = personalization_version
        for row in personalization_eligibility_rows():
            save_fact_eligibility_rule(
                session,
                personalization_version,
                row["fact_field"],
                row["exposure_mode"],
            )

        session.commit()
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--by", default="migrate_prompt_config")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    if not args.apply:
        print(json.dumps(build_seed(), indent=2, default=str))
        print(json.dumps({"personalization_policy_rules": personalization_eligibility_rows()}))
        return 0

    versions = apply_seed(args.by)
    print(json.dumps(versions, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
