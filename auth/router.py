"""
================================================================================
AUTH ROUTER — /auth/register, /auth/login, /auth/me
================================================================================
What this file does:
    POST /auth/login     — OAuth2 password flow (form fields: username=
                            email, password). Returns a JWT bearer token.
                            Using FastAPI's standard OAuth2PasswordRequestForm
                            shape (rather than a plain JSON body) means the
                            interactive /docs page's "Authorize" button
                            works out of the box, with no extra client code.
    POST /auth/register   — creates a user. Two modes, both handled by the
                            same endpoint:
                              - Database has zero users: open, unauthenticated
                                — the caller becomes the first admin. This is
                                the only way to bootstrap a brand-new
                                deployment (there is no other user yet who
                                could be asked to create one).
                              - Database already has at least one user:
                                admin-only — a valid admin bearer token is
                                required, and the role you request is
                                honoured.
                            (See also scripts/create_admin.py for creating/
                            resetting an admin from the command line without
                            going through the API at all.)
    GET  /auth/me         — returns the authenticated user's own profile.
================================================================================
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user, get_optional_current_user
from auth.security import create_access_token, hash_password, verify_password
from db.database import get_db
from db.models import User, UserRole

router = APIRouter(prefix="/auth", tags=["auth"])


class UserCreateRequest(BaseModel):
    email:        EmailStr
    password:     str           = Field(..., min_length=8)
    company_name: str           = Field(..., min_length=1)
    role:         str           = Field("client", description="'admin' or 'client'")
    client_id:    Optional[str] = Field(None, description="For role='client': which clients/<id>/ this user belongs to")


class UserOut(BaseModel):
    id:           int
    email:        str
    company_name: str
    role:         str
    client_id:    Optional[str]
    is_active:    bool

    class Config:
        from_attributes = True


class TokenOut(BaseModel):
    access_token: str
    token_type:   str = "bearer"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    request:      UserCreateRequest,
    db:           Session         = Depends(get_db),
    current_user: Optional[User]  = Depends(get_optional_current_user),
):
    is_first_user = db.query(User).count() == 0

    if not is_first_user and (current_user is None or current_user.role != UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to register new users",
        )

    if db.query(User).filter(User.email == request.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail=f"A user with email '{request.email}' already exists")

    try:
        role = UserRole(request.role)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role must be 'admin' or 'client'")

    # The very first user in an empty database always becomes admin — the
    # only way to bootstrap a brand-new deployment.
    if is_first_user:
        role = UserRole.ADMIN

    user = User(
        email           = request.email,
        hashed_password = hash_password(request.password),
        company_name    = request.company_name,
        role            = role,
        client_id       = request.client_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenOut)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form_data.username).first()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated")

    token = create_access_token(
        subject=user.email,
        extra_claims={"role": user.role.value, "client_id": user.client_id},
    )
    return TokenOut(access_token=token)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user
