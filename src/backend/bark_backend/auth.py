import re
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from . import user_store
from .env_util import resolve_env_secret

SECRET_KEY = resolve_env_secret(
    "BARK_JWT_SECRET", "bark-dev-secret-change-in-production"
)
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

MIN_PASSWORD_LENGTH = int(resolve_env_secret("BARK_MIN_PASSWORD_LENGTH", "4"))

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def create_token(
    user_id: str, email: str, roles: list[str] | None = None
) -> str:
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "jti": jti,
        "exp": expire,
        "roles": roles or [],
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


class RegisterResult(BaseModel):
    user_id: str
    email: str
    access_token: str | None = None


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(email: str) -> None:
    """Raise HTTPException if the email is not valid."""
    if not _EMAIL_RE.match(email):
        raise HTTPException(
            status_code=400, detail="Must be a valid email address"
        )


async def register(
    req: RegisterRequest, verified: bool = False
) -> RegisterResult:
    existing = await user_store.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    validate_email(req.email)
    if len(req.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    password_hash = hash_password(req.password)
    user = await user_store.create_user(
        req.email, password_hash, verified=verified
    )
    token = None
    if verified:
        roles = await user_store.get_user_roles(user["id"])
        token = create_token(user["id"], user["email"], roles)
    return RegisterResult(
        user_id=user["id"], email=user["email"], access_token=token
    )


async def login(req: LoginRequest) -> TokenResponse:
    user = await user_store.get_user_by_email(req.email)
    if user is None or not verify_password(
        req.password, user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.get("verified"):
        raise HTTPException(
            status_code=403, detail="Account not verified. Check your email."
        )

    roles = await user_store.get_user_roles(user["id"])
    token = create_token(user["id"], user["email"], roles)
    return TokenResponse(access_token=token)


VERIFY_TOKEN_EXPIRE_HOURS = 72
RESET_TOKEN_EXPIRE_HOURS = 1


def create_verification_token(user_id: str) -> str:
    """Create a JWT token for email verification."""
    expire = datetime.now(timezone.utc) + timedelta(
        hours=VERIFY_TOKEN_EXPIRE_HOURS
    )
    payload = {"sub": user_id, "purpose": "verify", "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_verification_token(token: str) -> str | None:
    """Decode a verification token. Returns user_id or None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "verify":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def create_password_reset_token(user_id: str) -> str:
    """Create a JWT token for password reset."""
    expire = datetime.now(timezone.utc) + timedelta(
        hours=RESET_TOKEN_EXPIRE_HOURS
    )
    payload = {"sub": user_id, "purpose": "reset", "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_password_reset_token(token: str) -> str | None:
    """Decode a password reset token. Returns user_id or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "reset":
            return None
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        if await user_store.is_token_blocklisted(jti):
            raise HTTPException(
                status_code=401, detail="Token has been revoked"
            )

        user = await user_store.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        user["roles"] = payload.get("roles", [])
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def require_role(role: str):
    """FastAPI dependency that requires the current user to have a specific role."""

    async def check(user: dict = Depends(get_current_user)) -> dict:
        if role not in user.get("roles", []):
            raise HTTPException(
                status_code=403, detail=f"Role '{role}' required"
            )
        return user

    return check


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Like get_current_user but returns None instead of raising 401."""
    if credentials is None:
        return None
    try:
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await user_store.is_token_blocklisted(jti):
            return None
        user = await user_store.get_user_by_id(user_id)
        if user is not None:
            user["roles"] = payload.get("roles", [])
        return user
    except JWTError:
        return None


async def get_user_from_token(token: str) -> dict | None:
    """Validate a token string (used for WebSocket auth)."""
    try:
        payload = decode_token(token)
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
        payload = decode_token(token)
        jti = payload.get("jti")
        exp = payload.get("exp")
        if jti and exp:
            expires_at = datetime.fromtimestamp(
                exp, tz=timezone.utc
            ).isoformat()
            await user_store.blocklist_token(jti, expires_at)
    except JWTError:
        pass
