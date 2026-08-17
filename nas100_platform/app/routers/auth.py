from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

import auth
from db import get_db
from models import User, UserSettings
from rate_limit import limiter
from schemas import ChangePasswordRequest, LoginRequest, SignupRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=UserOut)
@limiter.limit("5/minute")
def signup(request: Request, body: SignupRequest, response: Response, db: Session = Depends(get_db)):
    if not body.accept_tos:
        raise HTTPException(status_code=400, detail="You must accept the risk disclosure to create an account")

    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=body.email.lower(),
        password_hash=auth.hash_password(body.password),
        accepted_tos_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.commit()
    db.refresh(user)

    auth.set_session_cookie(response, user.id)
    return UserOut(id=user.id, email=user.email)


@router.post("/login", response_model=UserOut)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    # Constant-shape error whether the email exists or the password is
    # wrong, so the endpoint doesn't leak which emails are registered.
    if user is None or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been disabled")

    auth.set_session_cookie(response, user.id)
    return UserOut(id=user.id, email=user.email)


@router.post("/logout")
def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"status": "ok"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(auth.get_current_user)):
    return UserOut(id=user.id, email=user.email)


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    user.password_hash = auth.hash_password(body.new_password)
    db.commit()
    return {"status": "ok"}
