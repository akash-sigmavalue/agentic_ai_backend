from typing import Annotated
from types import SimpleNamespace
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from auth import repositories
from core.config import settings
from database.db import get_db
from database.connector import crud
from api.schemas.ui_creation import user as user_schema

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)
AUTH_DISABLED_USER = SimpleNamespace(id=1, username="auth_disabled")


async def get_current_user(
    token: Annotated[str | None, Depends(optional_oauth2_scheme)] = None,
    db: Session = Depends(get_db),
):
    # Authentication/authorization is disabled. Keep returning a stable user-like object
    # for code paths that still expect current_user.id/current_user.username.
    return AUTH_DISABLED_USER


async def get_current_user_with_jwt(token: Annotated[str, Depends(oauth2_scheme)], db: Session = Depends(get_db)):
    # Original JWT-based authentication, kept for easy rollback.
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = user_schema.TokenData(username=username)
    except JWTError:
        raise credentials_exception
        
    user = repositories.get_user_by_username(db, username=token_data.username)
    if user is None:
        raise credentials_exception
    return user


async def get_optional_current_user(
    token: Annotated[str | None, Depends(optional_oauth2_scheme)],
    db: Session = Depends(get_db),
):
    # Authentication/authorization is disabled.
    return AUTH_DISABLED_USER

    # Original optional JWT-based authentication, kept for easy rollback.
    # if not token:
    #     return None
    #
    # return await get_current_user_with_jwt(token, db)
