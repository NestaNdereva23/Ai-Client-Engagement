from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.auth.passwords import hash_password  # noqa: E402
from app.db.models.auth import REVIEWER_ROLES, ReviewerUser  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update one reviewer console login. Prompts for the "
            "password rather than taking it as an argument, so it never "
            "lands in shell history."
        )
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--role", required=True, choices=REVIEWER_ROLES)
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1
    if not password:
        print("Password may not be blank.", file=sys.stderr)
        return 1

    with SessionLocal() as session:
        user = session.scalar(select(ReviewerUser).where(ReviewerUser.username == args.username))
        action = "updated"
        if user is None:
            user = ReviewerUser(username=args.username)
            session.add(user)
            action = "created"

        user.display_name = args.display_name
        user.role = args.role
        user.password_hash = hash_password(password)
        user.active = True
        session.commit()

    print(f"{action} reviewer_user '{args.username}' ({args.role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
