from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

# from app.auth.dependencies import get_current_user
from agents.connector.services.google_oauth import refresh_google_access_token
# Authentication/authorization is currently disabled.
# from auth.connector.dependencies import get_current_user
from database.connector import crud
from database import db
# from app.google_api.gmail_client import GmailAPIClient, GmailAPIError
# from app.services.google_oauth import refresh_google_access_token
from utils.connector.gmail_client import GmailAPIClient, GmailAPIError
router = APIRouter(prefix="/debug", tags=["debug"])
AUTH_DISABLED_USER_ID = 1


def _mask_token(token: str) -> str:
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}...{token[-4:]}"


@router.get("/gmail-token-test")
async def gmail_token_test(
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
        return {"status": "no_token"}

    try:
        access_token = connection.access_token
        if connection.expires_at is not None and connection.expires_at <= datetime.now(timezone.utc):
            access_token = await _refresh_google_token(db, AUTH_DISABLED_USER_ID, connection)

        return await _run_gmail_debug_checks(
            access_token=access_token,
            connected_email=connection.email,
        )
    except GmailAPIError as exc:
        if exc.status_code == 401 and connection.refresh_token:
            refreshed_access_token = await _refresh_google_token(db, AUTH_DISABLED_USER_ID, connection)
            try:
                return await _run_gmail_debug_checks(
                    access_token=refreshed_access_token,
                    connected_email=connection.email,
                )
            except GmailAPIError as retry_exc:
                return {
                    "status": "failed",
                    "error": str(retry_exc),
                    "status_code": retry_exc.status_code,
                    "token_preview": _mask_token(refreshed_access_token),
                }

        return {
            "status": "failed",
            "error": str(exc),
            "status_code": exc.status_code,
            "token_preview": _mask_token(connection.access_token),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "token_preview": _mask_token(connection.access_token),
        }


async def _run_gmail_debug_checks(
    *,
    access_token: str,
    connected_email: str | None,
) -> dict[str, object]:
    profile_data = await _fetch_gmail_profile(access_token)
    gmail_client = GmailAPIClient()
    search_data = await gmail_client.search_threads(access_token, max_results=5)

    return {
        "status": "success",
        "connected_email": connected_email,
        "profile_email": profile_data.get("emailAddress") or connected_email,
        "search_threads_count": len(search_data.get("threads") or []),
        "search_threads": search_data,
        "token_preview": _mask_token(access_token),
    }


async def _fetch_gmail_profile(access_token: str) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers=headers,
        )

    if response.status_code >= 400:
        raise GmailAPIError(response.status_code, response.text, response.text)

    data = response.json()
    return data if isinstance(data, dict) else {"result": data}


async def _refresh_google_token(db: Session, user_id: int, connection) -> str:
    if not connection.refresh_token:
        return connection.access_token

    refreshed = await refresh_google_access_token(connection.refresh_token)
    refreshed_access_token = str(refreshed.get("access_token") or "")
    if not refreshed_access_token:
        return connection.access_token

    expires_in = int(refreshed.get("expires_in") or 0)
    expires_at = None
    if expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    crud.upsert_oauth_connection(
        db=db,
        user_id=user_id,
        provider="google",
        system="gmail",
        email=connection.email,
        access_token=refreshed_access_token,
        refresh_token=str(refreshed.get("refresh_token")) if refreshed.get("refresh_token") else connection.refresh_token,
        expires_at=expires_at or connection.expires_at,
        scope=str(refreshed.get("scope")) if refreshed.get("scope") else connection.scope,
        token_type=str(refreshed.get("token_type")) if refreshed.get("token_type") else connection.token_type,
    )
    return refreshed_access_token
