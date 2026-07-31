"""
================================================================================
CREATE / RESET AN ADMIN USER — command-line, bypasses the API entirely
================================================================================
What this does:
    A production ops convenience for Railway (`railway run python
    scripts/create_admin.py ...`) or any environment with DATABASE_URL set:
    creates a new admin user, or resets an existing user's password to
    admin if the email is already registered. Doesn't require going
    through POST /auth/register (which needs an existing admin's token,
    or an empty database) — useful for recovering access if every admin
    account is locked out.

Usage:
    python scripts/create_admin.py <email> "<company name>" [password]

    If password is omitted, you'll be prompted for it (hidden input).
================================================================================
"""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import Base, SessionLocal, engine
from db.models import User, UserRole
from auth.security import hash_password


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: python {sys.argv[0]} <email> \"<company name>\" [password]")
        sys.exit(1)

    email        = sys.argv[1]
    company_name = sys.argv[2]
    password     = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("Password: ")

    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(
                email=email, company_name=company_name,
                hashed_password=hash_password(password),
                role=UserRole.ADMIN, is_active=True,
            )
            db.add(user)
            action = "Created"
        else:
            user.hashed_password = hash_password(password)
            user.role = UserRole.ADMIN
            user.is_active = True
            action = "Reset"
        db.commit()
        print(f"{action} admin user: {email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
