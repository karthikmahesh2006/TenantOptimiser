"""Authentication, password hashing, and session management."""

import os
import secrets
from typing import Optional

from argon2 import PasswordHasher
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from alloc_platform.config import config
from alloc_platform.db.models import User
from alloc_platform.db.repo import log_audit
from alloc_platform.db.session import SessionLocal, get_db

ph = PasswordHasher()
serializer = URLSafeTimedSerializer(config.secret_key)


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return ph.verify(hashed, password)
    except Exception:
        return False


def create_session_token(username: str, role: str) -> str:
    return serializer.dumps({"username": username, "role": role})


def verify_session_token(token: str) -> Optional[dict]:
    try:
        data = serializer.loads(token, max_age=config.session_max_age_s)
        return data
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get(config.session_cookie_name)
    if not token:
        return None
    data = verify_session_token(token)
    if not data:
        return None
    user = db.query(User).filter(User.username == data.get("username")).first()
    return user


def require_auth(user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return user


def require_admin(user: User = Depends(require_auth)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required"
        )
    return user


def bootstrap_admin_user() -> Optional[str]:
    """Ensures at least one admin user exists.

    If none, generates a secure random password and saves it to bootstrap_admin.txt.
    """
    db: Session = SessionLocal()
    try:
        admin = db.query(User).filter(User.role == "admin").first()
        if admin:
            return None

        password = os.getenv("ALLOC_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
        pw_hash = hash_password(password)
        new_admin = User(username="admin", password_hash=pw_hash, role="admin")
        db.add(new_admin)
        db.commit()

        # Determine path
        target_path = "/etc/alloc/bootstrap_admin.txt"
        if not os.path.exists("/etc/alloc"):
            target_path = "bootstrap_admin.txt"

        try:
            with open(target_path, "w") as f:
                f.write(f"Username: admin\nPassword: {password}\n")
            try:
                os.chmod(target_path, 0o600)
            except Exception:
                pass
        except Exception:
            pass

        log_audit(db, "bootstrap_admin_created", details="Generated initial admin credentials.")
        return password
    finally:
        db.close()
