"""Sets a deterministic admin password for demo access."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.api.auth import hash_password
from alloc_platform.db.models import User
from alloc_platform.db.session import SessionLocal


def set_admin_password(password: str = "AdminAlloc2026!"):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "admin").first()
        if not user:
            user = User(username="admin", role="admin")
            db.add(user)
        user.password_hash = hash_password(password)
        db.commit()
        print(f"Admin password successfully set to: {password}")
    finally:
        db.close()


if __name__ == "__main__":
    pw = sys.argv[1] if len(sys.argv) > 1 else "AdminAlloc2026!"
    set_admin_password(pw)
