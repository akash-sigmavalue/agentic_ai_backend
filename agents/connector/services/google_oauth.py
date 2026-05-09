from __future__ import annotations

from urllib.parse import urlencode

import httpx

from core.config import settings

# from core.config import settings


GOOGLE_PROVIDER = "google"
GMAIL_SYSTEM = "gmail"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_GMAIL_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
)


def build_google_authorization_url(state: str) -> str:
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_REDIRECT_URI:
        raise ValueError("Google OAuth settings are not configured")

    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }

    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


async def exchange_google_code_for_token(code: str) -> dict[str, object]:
    if (
        not settings.GOOGLE_OAUTH_CLIENT_ID
        or not settings.GOOGLE_OAUTH_CLIENT_SECRET
        or not settings.GOOGLE_OAUTH_REDIRECT_URI
    ):
        raise ValueError("Google OAuth settings are not configured")

    data = {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)

    response.raise_for_status()
    return response.json()


async def refresh_google_access_token(refresh_token: str) -> dict[str, object]:
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise ValueError("Google OAuth settings are not configured")

    data = {
        "refresh_token": refresh_token,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)

    response.raise_for_status()
    return response.json()


async def fetch_google_userinfo(access_token: str) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(GOOGLE_USERINFO_ENDPOINT, headers=headers)

    response.raise_for_status()
    return response.json()


async def debug_google_token(access_token: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v3/tokeninfo",
            params={"access_token": access_token},
        )

    response.raise_for_status()
    return response.json()
