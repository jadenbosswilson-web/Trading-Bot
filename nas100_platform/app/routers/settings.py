from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import crypto
from db import get_db
from liquidcharts_client import LiquidChartsClient, LiquidChartsError
from models import BrokerCredential, User, UserSettings
from schemas import BrokerCredentialIn, GoLiveRequest, SettingsIn, SettingsOut

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
    return SettingsOut(
        **{k: getattr(s, k) for k in SettingsIn.model_fields},
        dry_run=s.dry_run,
        has_broker_credentials=user.broker_credential is not None,
    )


@router.put("", response_model=SettingsOut)
def update_settings(body: SettingsIn, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    s = _get_or_create_settings(db, user)
    for field, value in body.model_dump().items():
        setattr(s, field, value)
    db.commit()
    return SettingsOut(
        **body.model_dump(),
        dry_run=s.dry_run,
        has_broker_credentials=user.broker_credential is not None,
    )


@router.put("/broker")
def save_broker_credentials(
    body: BrokerCredentialIn, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    cred = user.broker_credential
    if cred is None:
        cred = BrokerCredential(user_id=user.id, username_enc="", password_enc="", domain_enc="", account_code_enc="")
        db.add(cred)

    cred.username_enc = crypto.encrypt(body.username)
    cred.password_enc = crypto.encrypt(body.password)
    cred.domain_enc = crypto.encrypt(body.domain)
    cred.account_code_enc = crypto.encrypt(body.account_code)
    cred.last_verified_at = None
    db.commit()
    return {"status": "saved", "note": "Credentials stored (encrypted). Use /test to verify them before going live."}


@router.post("/broker/test")
def test_broker_credentials(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    cred = user.broker_credential
    if cred is None:
        raise HTTPException(status_code=400, detail="No broker credentials saved yet")

    client = LiquidChartsClient(
        username=crypto.decrypt(cred.username_enc),
        password=crypto.decrypt(cred.password_enc),
        domain=crypto.decrypt(cred.domain_enc),
        account_code=crypto.decrypt(cred.account_code_enc),
    )
    try:
        client.login()
    except LiquidChartsError as e:
        raise HTTPException(status_code=400, detail=f"Login failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Liquid Charts: {e}")

    from datetime import datetime, timezone
    cred.last_verified_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "ok", "message": "Login succeeded — credentials are valid."}


@router.delete("/broker")
def delete_broker_credentials(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    if user.broker_credential is not None:
        db.delete(user.broker_credential)
    s = _get_or_create_settings(db, user)
    s.dry_run = True  # can't go live with no credentials
    db.commit()
    return {"status": "deleted"}


@router.put("/go-live")
def set_go_live(body: GoLiveRequest, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    s = _get_or_create_settings(db, user)
    if not body.dry_run and user.broker_credential is None:
        raise HTTPException(status_code=400, detail="Add and verify broker credentials before disabling dry-run mode")
    s.dry_run = body.dry_run
    db.commit()
    return {"dry_run": s.dry_run}
