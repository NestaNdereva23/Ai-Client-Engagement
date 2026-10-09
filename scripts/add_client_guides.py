from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.rag.guides import add_guide, list_guide_versions  # noqa: E402

GUIDES = (
    (
        "How the monthly fee works",
        "Fees",
        "A monthly fee of KES 50 applies to your account. On a small balance this can add "
        "up over time. Paying in a little from time to time helps your balance stay ahead "
        "of the fee.",
        ("fee_warning", "fee_pressure_warning_dormant", "fee_pressure_encourage_active"),
    ),
    (
        "What a money market fund is",
        "Products",
        "A money market fund pools money from many investors and lends it out for short "
        "periods, for example by buying treasury bills or placing it in bank deposits. "
        "The aim is to keep your money safe and easy to reach while it earns a steady "
        "return. The return moves with market rates and is never guaranteed.",
        ("send_learning_note",),
    ),
    (
        "Why some investors hold more than one fund",
        "Funds",
        "Different funds are built for different jobs. Some are meant for money you may "
        "need soon, and others for money you can leave for longer. Many investors hold "
        "more than one fund so that each part of their savings has a clear purpose. "
        "Knowing what each fund is for makes it easier to choose, and there is never "
        "any rush.",
        ("suggest_second_fund", "send_learning_note"),
    ),
    (
        "Why small regular deposits beat one large one",
        "Saving habit",
        "Many investors find that small deposits made regularly work better than waiting "
        "to invest one large amount. You do not have to guess the right moment, and a "
        "small habit is easier to keep than a big payment. Over time the small deposits "
        "add up.",
        ("welcome_and_top_up", "send_learning_note"),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add the first client guides. Each one waits for a person to approve it."
    )
    parser.add_argument("--by", required=True, help="Who is adding them.")
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        on_file = {
            (guide.title.lower(), guide.text, guide.action_codes)
            for guide in list_guide_versions(session)
        }
        for title, topic, text, actions in GUIDES:
            if (title.lower(), text, tuple(sorted(actions))) in on_file:
                print(f"already on file: {title}")
                continue
            guide = add_guide(
                session,
                title=title,
                topic=topic,
                text=text,
                action_codes=actions,
                created_by=args.by,
            )
            print(f"waiting for approval: {title} (version {guide.version_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
