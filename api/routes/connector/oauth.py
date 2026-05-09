from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

# from app.auth.dependencies import get_current_user
from agents.connector.services.gmail_pubsub import GmailPubSubService
from api.routes.connector.auth import _decode_google_state,_build_google_state
# Authentication/authorization is currently disabled.
# from auth.connector.dependencies import get_current_user
from database.connector import crud
# from app.db.database import get_db
# from app.services.gmail_pubsub import GmailPubSubService
from agents.connector.services.google_oauth import (
    build_google_authorization_url,
    debug_google_token,
    exchange_google_code_for_token,
    fetch_google_userinfo,
)
from database import db


router = APIRouter(prefix="/oauth", tags=["oauth"])
AUTH_DISABLED_USER_ID = 1


@router.get("/google/start")
def start_google_oauth(
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, str]:
    state = _build_google_state(AUTH_DISABLED_USER_ID)

    try:
        authorization_url = build_google_authorization_url(state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return {"auth_url": authorization_url}


@router.get("/google/callback")
async def google_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(db.get_db),
):
    try:
        token_data = await exchange_google_code_for_token(code)

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = int(token_data.get("expires_in", 3600))
        scope = token_data.get("scope")

        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google did not return access_token",
            )

        userinfo = await fetch_google_userinfo(access_token)
        email = userinfo.get("email")

        user_id = _decode_google_state(state)
        
        print("=== GOOGLE OAUTH CALLBACK ===")
        print("decoded user_id:", user_id)
        print("google email:", email)
        print("scope:", scope)
        print("access token exists:", bool(access_token))
        print("refresh token exists:", bool(refresh_token))

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        crud.upsert_oauth_connection(
            db=db,
            user_id=user_id,
            provider="google",
            system="gmail",
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scope=scope,
        )

        connection = crud.get_oauth_connection(
            db,
            user_id=user_id,
            provider="google",
            system="gmail",
        )
        if connection is not None:
            await GmailPubSubService().ensure_watch(db, connection=connection)

        return {
            "success": True,
            "message": "Google Gmail connected successfully",
            "email": email,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/google/debug-token")
async def debug_google_oauth_token(
    db: Session = Depends(db.get_db),
    # Authentication/authorization is disabled.
    # current_user=Depends(get_current_user),
) -> dict[str, object]:
    connection = crud.get_oauth_connection(
        db,
        user_id=AUTH_DISABLED_USER_ID,
        provider="google",
        system="gmail",
    )

    if connection is None or not connection.access_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google Gmail OAuth connection not found",
        )

    try:
        token_info = await debug_google_token(connection.access_token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to inspect Google OAuth token",
        ) from exc

    # Safe debug output only: no tokens are returned.
    return {
        "success": True,
        "provider": "google",
        "system": "gmail",
        "email": connection.email,
        "scope": token_info.get("scope"),
        "expires_in": token_info.get("expires_in"),
        "issued_to": token_info.get("issued_to"),
        "audience": token_info.get("audience"),
        "azp": token_info.get("azp"),
        "verified_email": token_info.get("email_verified"),
        "raw": {
            key: value
            for key, value in token_info.items()
            if key not in {"access_token", "refresh_token"}
        },
    }
