"""
================================================================================
AUTH — password hashing and JWT creation/verification
================================================================================
What this file does:
    Two independent primitives used by auth/router.py and
    auth/dependencies.py:
        1. Password hashing — bcrypt directly (not passlib — passlib's
           bcrypt backend has had version-detection breakage against
           recent bcrypt releases; calling bcrypt.hashpw/checkpw directly
           avoids that dependency entirely and is all this needs).
        2. JWT access tokens — PyJWT, HS256, signed with SECRET_KEY.

SECRET_KEY handling:
    Read from the environment. If unset, falls back to an obviously-fake
    development key and prints a loud warning — this fallback exists only
    so the API boots without a .env file for local development; deploying
    to Railway without setting SECRET_KEY would sign every token with a
    publicly-known string, so .env.example documents this as required.
================================================================================
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = "INSECURE-DEV-ONLY-SECRET-DO-NOT-USE-IN-PRODUCTION"
    print("WARNING: SECRET_KEY not set — using an insecure development default. "
          "Set SECRET_KEY in production (see .env.example).")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_access_token(subject: str, extra_claims: Optional[Dict[str, Any]] = None,
                         expires_minutes: Optional[int] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes or ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Raises a jwt.PyJWTError (or subclass — ExpiredSignatureError, InvalidTokenError, ...) if invalid."""
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
