"""Sets a portal account's password from the command line.

Used to rotate a password without it crossing the network at all — not even
over TLS. getpass keeps it off the screen, out of shell history and out of
the logs, so the only place it ever exists is the argon2 hash in the
database.

    python -m portal.setpassword admin@example.com
"""
import getpass
import sys

from portal.auth import hash_password
from storage import db

MIN_LENGTH = 10


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python -m portal.setpassword <email>")

    email = sys.argv[1]
    db.init_db()
    user = db.get_portal_user_by_email(email)
    if not user:
        sys.exit(f"No portal account for {email}")

    print(f"Setting a new password for {user['name']} <{user['email']}> ({user['role']})")
    password = getpass.getpass("New password: ")
    if len(password) < MIN_LENGTH:
        sys.exit(f"Use at least {MIN_LENGTH} characters")
    if password != getpass.getpass("Confirm: "):
        sys.exit("Passwords don't match")

    # must_change stays False: this person chose it themselves, so there is
    # nothing left to force on them at the next sign-in.
    db.set_portal_password(user["id"], hash_password(password))
    print("Password updated.")


if __name__ == "__main__":
    main()
