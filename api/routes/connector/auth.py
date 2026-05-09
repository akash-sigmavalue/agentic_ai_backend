from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt as jose_jwt
from sqlalchemy.orm import Session

from agents.connector.services.gmail_pubsub import GmailPubSubService
from auth import security, jwt
# Authentication/authorization is currently disabled.
# from auth.connector.dependencies import get_current_user
from database.connector import crud
from database import db
from api.schemas.ui_creation import user as user_schema
from core.config import settings
# from app.auth.dependencies import get_current_user
from agents.connector.services.google_oauth import (
    GMAIL_SYSTEM,
    GOOGLE_PROVIDER,
    build_google_authorization_url,
    exchange_google_code_for_token,
    fetch_google_userinfo,
)

from fastapi import APIRouter

def _build_google_state(user_id: int) -> str:
    return jose_jwt.encode(
        {"sub": str(user_id), "provider": GOOGLE_PROVIDER, "system": GMAIL_SYSTEM},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def _decode_google_state(state: str) -> int:
    try:
        payload = jose_jwt.decode(state, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google OAuth state") from exc

    if payload.get("provider") != GOOGLE_PROVIDER or payload.get("system") != GMAIL_SYSTEM:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google OAuth state")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google OAuth state")
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google OAuth state") from exc



router = APIRouter(prefix="/auth", tags=["auth"])
AUTH_DISABLED_USER_ID = 1

@router.post("/register", response_model=user_schema.User)
def register(user: user_schema.UserCreate, db: Session = Depends(db.get_db)):
    db_user = crud.get_user_by_username(db, username=user.username)
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    return crud.create_user(db=db, user=user)

@router.post("/login", response_model=user_schema.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(db.get_db)):
    user = crud.get_user_by_username(db, username=form_data.username)
    if not user or not security.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = jwt.create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/google/start")
def start_google_oauth(
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
):
    state = _build_google_state(AUTH_DISABLED_USER_ID)
    try:
        authorization_url = build_google_authorization_url(state)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
    return {"success": True, "authorization_url": authorization_url}


@router.get("/google/callback")
async def google_oauth_callback(
    code: str,
    state: str,
    db: Session = Depends(db.get_db),
):
    user_id = _decode_google_state(state)

    try:
        token_response = await exchange_google_code_for_token(code)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to exchange Google OAuth code") from exc

    access_token = token_response.get("access_token")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google OAuth did not return an access token")

    refresh_token = token_response.get("refresh_token")
    token_type = token_response.get("token_type")
    scope = token_response.get("scope")
    expires_in = int(token_response.get("expires_in") or 0)

    email = None
    try:
        userinfo = await fetch_google_userinfo(str(access_token))
        email = userinfo.get("email")
    except Exception:
        email = None

    from datetime import datetime, timedelta, timezone

    expires_at = None
    if expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    crud.upsert_oauth_connection(
        db,
        user_id=user_id,
        provider=GOOGLE_PROVIDER,
        system=GMAIL_SYSTEM,
        email=email,
        access_token=str(access_token),
        refresh_token=str(refresh_token) if refresh_token else None,
        token_type=str(token_type) if token_type else None,
        expires_at=expires_at,
        scope=str(scope) if scope else None,
    )

    connection = crud.get_oauth_connection(
        db,
        user_id=user_id,
        provider=GOOGLE_PROVIDER,
        system=GMAIL_SYSTEM,
    )
    if connection is not None:
        await GmailPubSubService().ensure_watch(db, connection=connection)

    return {
        "success": True,
        "message": "Google Gmail connected successfully",
        "email": email,
    }
