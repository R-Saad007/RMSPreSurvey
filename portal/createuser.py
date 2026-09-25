"""Creates a portal account from the command line.

Needed once to make the first admin — after that, accounts are added from
the Accounts page.

    python -m portal.createuser admin@example.com "Admin" hq_admin
"""
import getpass
import sys

from portal.auth import ROLES, hash_password
from storage import db


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: python -m portal.createuser <email> <name> [hq_admin|hq_staff]")

    email, name = sys.argv[1], sys.argv[2]
    role = sys.argv[3] if len(sys.argv) > 3 else "hq_staff"
    if role not in ROLES:
        sys.exit(f"role must be one of: {', '.join(ROLES)}")
    if role == "company_manager":
        # A company manager without a company would see nothing at all. Those
        # accounts are made from the Accounts page, where a company is chosen.
        sys.exit("Company managers are created from the Accounts page (they need a company). "
                 "This tool makes hq_admin and hq_staff logins.")

    db.init_db()
    if db.get_portal_user_by_email(email):
        sys.exit(f"{email} already has an account")

    password = getpass.getpass("Password: ")
    if len(password) < 8:
        sys.exit("Use at least 8 characters")
    if password != getpass.getpass("Confirm: "):
        sys.exit("Passwords don't match")

    # Chosen at the keyboard just now, so there's nothing to force a change of.
    db.create_portal_user(email, hash_password(password), name, role, must_change=False)
    print(f"Created {role} account for {name} <{email}>")


if __name__ == "__main__":
    main()
