from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import auth
from db import get_db
from models import User, UserSettings
from schemas import SettingsIn, SettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _get_or_create_settings(db: Session, user: User) -> UserSettings:
    if user.settings is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        db.commit()
        db.refresh(user)
    return user.settings


@router.get("", response_model=SettingsOut)
def get_settings(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    s = _get_or_create_settings(db, user)
    return SettingsOut(**{k: getattr(s, k) for k in SettingsIn.model_fields})


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsIn, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    s = _get_or_create_settings(db, user)
    for field, value in body.model_dump().items():
        setattr(s, field, value)
    db.commit()
    return SettingsOut(**body.model_dump())
