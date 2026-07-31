"""
================================================================================
AUTH — FastAPI dependencies for protected routes
================================================================================
What this file does:
    get_current_user       — required auth: decodes the bearer token,
                              loads the User, 401s if missing/invalid/
                              inactive. Used as the dependency on every
                              protected route (see api/main.py's
                              `protected` router).
    get_current_admin      — same, plus 403s if role != admin. Used on
                              admin-only routes (user management).
    get_optional_current_user — like get_current_user but returns None
                              instead of raising when no token is
                              presented — needed only by
                              auth/router.py's /auth/register, which must
                              work both unauthenticated (bootstrapping the
                              very first admin in an empty database) and
                              admin-authenticated (every registration after
                              that).
================================================================================
"""

from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from auth.security import decode_access_token
from db.database import get_db
from db.models import User, UserRole

oauth2_scheme          = OAuth2PasswordBearer(tokenUrl="/auth/login")
oauth2_scheme_optional  = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _user_from_token(token: str, db: Session) -> Optional[User]:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None
    email = payload.get("sub")
    if not email:
        return None
    return db.query(User).filter(User.email == email).first()


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    user = _user_from_token(token, db)
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXCEPTION
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return current_user


def get_optional_current_user(
    token: Optional[str] = Depends(oauth2_scheme_optional), db: Session = Depends(get_db)
) -> Optional[User]:
    if not token:
        return None
    user = _user_from_token(token, db)
    return user if (user is not None and user.is_active) else None
