from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import or_, select  # noqa: E402

from app.audit.log import record_audit  # noqa: E402
from app.db.models.test_recipient import TestRecipient  # noqa: E402
from app.db.session import restricted_session  # noqa: E402


def _find(session, email: str | None, phone: str | None) -> TestRecipient | None:
    filters = []
    if email:
        filters.append(TestRecipient.email == email)
    if phone:
        filters.append(TestRecipient.phone == phone)
    return session.scalar(select(TestRecipient).where(or_(*filters)))


def add(args: argparse.Namespace) -> int:
    if not args.email and not args.phone:
        print("Give an --email, a --phone, or both.", file=sys.stderr)
        return 1
    with restricted_session() as session:
        recipient = _find(session, args.email, args.phone)
        action = "updated"
        if recipient is None:
            recipient = TestRecipient(name=args.name)
            session.add(recipient)
            action = "created"
        recipient.name = args.name
        recipient.email = args.email or recipient.email
        recipient.phone = args.phone or recipient.phone
        recipient.active = True
        session.flush()
        record_audit(
            session,
            entity_type="test_recipient",
            action=action,
            entity_id=str(recipient.recipient_id),
        )
        session.commit()
        print(f"{action} test recipient {recipient.recipient_id} ({recipient.name})")
    return 0


def remove(args: argparse.Namespace) -> int:
    with restricted_session() as session:
        recipient = _find(session, args.email, args.phone)
        if recipient is None:
            print("No test recipient with that email or phone.", file=sys.stderr)
            return 1
        recipient.active = False
        record_audit(
            session,
            entity_type="test_recipient",
            action="deactivated",
            entity_id=str(recipient.recipient_id),
        )
        session.commit()
        print(f"deactivated test recipient {recipient.recipient_id} ({recipient.name})")
    return 0


def show(_: argparse.Namespace) -> int:
    with restricted_session() as session:
        rows = session.scalars(select(TestRecipient).order_by(TestRecipient.recipient_id))
        for r in rows:
            state = "active" if r.active else "inactive"
            print(f"{r.recipient_id}\t{state}\t{r.name}\t{r.email or '-'}\t{r.phone or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage the team inboxes and phones that receive test campaign sends."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    add_cmd = commands.add_parser("add", help="Add or reactivate a tester.")
    add_cmd.add_argument("--name", required=True)
    add_cmd.add_argument("--email")
    add_cmd.add_argument("--phone")
    add_cmd.set_defaults(run=add)

    remove_cmd = commands.add_parser("remove", help="Stop sending to a tester.")
    remove_cmd.add_argument("--email")
    remove_cmd.add_argument("--phone")
    remove_cmd.set_defaults(run=remove)

    list_cmd = commands.add_parser("list", help="Show every tester.")
    list_cmd.set_defaults(run=show)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
