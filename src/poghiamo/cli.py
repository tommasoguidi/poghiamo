"""Admin command line, for what the web UI cannot do. Run it on the server:

    docker compose exec web poghiamo-admin reset-link <username>
"""

import argparse
import sys

from poghiamo.config import WEBAPP_URL
from poghiamo.database.engine import SessionLocal, init_db
from poghiamo.database.models import User
from poghiamo.services import password_reset


def reset_link(username: str) -> int:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            print(f"No user named {username!r}.", file=sys.stderr)
            return 1
        token = password_reset.issue(db, user)
        db.commit()
        print(f"Reset link for {user.username}, valid 72 hours, single use:")
        print(password_reset.reset_url(WEBAPP_URL, token))
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="poghiamo-admin", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser(
        "reset-link",
        help="print a password reset link for a user (also the way back in for a locked-out admin)",
    )
    cmd.add_argument("username")
    args = parser.parse_args(argv)

    # Same schema setup as the web app, so the command also works on a fresh image.
    init_db()
    if args.command == "reset-link":
        return reset_link(args.username)
    return 2


if __name__ == "__main__":
    sys.exit(main())
