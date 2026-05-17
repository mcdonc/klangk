import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from . import user_store

SECRET_KEY = os.environ.get("BARK_JWT_SECRET", "bark-dev-secret-change-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

security = HTTPBearer(auto_error=False)


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _create_token(user_id: str, username: str) -> str:
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "username": username,
        "jti": jti,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


async def register(req: RegisterRequest) -> TokenResponse:
    existing = await user_store.get_user_by_username(req.username)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Username already taken")
    if len(req.username) < 3:
        raise HTTPException(
            status_code=400, detail="Username must be at least 3 characters"
        )
    if len(req.password) < 4:
        raise HTTPException(
            status_code=400, detail="Password must be at least 4 characters"
        )

    password_hash = _hash_password(req.password)
    user = await user_store.create_user(req.username, password_hash)
    token = _create_token(user["id"], user["username"])
    return TokenResponse(access_token=token)


async def login(req: LoginRequest) -> TokenResponse:
    user = await user_store.get_user_by_username(req.username)
    if user is None or not _verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_token(user["id"], user["username"])
    return TokenResponse(access_token=token)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = _decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        if await user_store.is_token_blocklisted(jti):
            raise HTTPException(status_code=401, detail="Token has been revoked")

        user = await user_store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Like get_current_user but returns None instead of raising 401."""
    if credentials is None:
        return None
    try:
        payload = _decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await user_store.is_token_blocklisted(jti):
            return None
        return await user_store.get_user_by_id(user_id)
    except JWTError:
        return None


async def get_user_from_token(token: str) -> dict | None:
    """Validate a token string (used for WebSocket auth)."""
    try:
        payload = _decode_token(token)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await user_store.is_token_blocklisted(jti):
            return None
        return await user_store.get_user_by_id(user_id)
    except JWTError:
        return None


async def logout(token: str) -> None:
    """Blocklist the token's JTI."""
    try:
        payload = _decode_token(token)
        jti = payload.get("jti")
        exp = payload.get("exp")
        if jti and exp:
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
            await user_store.blocklist_token(jti, expires_at)
    except JWTError:
        pass
