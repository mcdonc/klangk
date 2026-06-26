import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel

from . import model
from .exceptions import ConfigurationError
from .util import resolve_env_secret

logger = logging.getLogger(__name__)

# --- Rate limiting constants ---
LOGIN_LOCKOUT_WINDOW = int(
    resolve_env_secret("KLANGK_LOGIN_LOCKOUT_WINDOW", "300")
)
LOGIN_LOCKOUT_DURATION = int(
    resolve_env_secret("KLANGK_LOGIN_LOCKOUT_DURATION", "900")
)


def _is_locked_out(
    attempt_info: dict | None,
) -> tuple[bool, str | None]:
    """Check if an email is locked out.

    Returns (is_locked, error_message).
    """
    if attempt_info is None:
        return False, None
    locked_until = attempt_info.get("locked_until")
    if locked_until is None:
        return False, None
    locked_dt = datetime.fromisoformat(locked_until)
    if datetime.now(timezone.utc) < locked_dt:
        remaining = int(
            (locked_dt - datetime.now(timezone.utc)).total_seconds()
        )
        return (
            True,
            f"Too many failed attempts. Try again in {remaining // 60} minutes.",
        )
    return False, None


def _should_lockout(attempt_info: dict | None) -> bool:
    """Return True if the attempt count exceeds the threshold."""
    if attempt_info is None:
        return False
    return (
        attempt_info.get("attempt_count", 0) >= LOGIN_LOCKOUT_FAILURES
    )  # pragma: no cover


_INSECURE_DEFAULT_SECRET = "klangk-dev-secret-change-in-production"
SECRET_KEY = resolve_env_secret("KLANGK_JWT_SECRET", _INSECURE_DEFAULT_SECRET)
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = float(
    resolve_env_secret("KLANGK_ACCESS_TOKEN_HOURS", "24")
)


def jwt_secret_is_secure() -> bool:
    """True if a non-empty, non-default JWT signing secret is configured."""
    return bool(SECRET_KEY) and SECRET_KEY != _INSECURE_DEFAULT_SECRET


def require_secure_jwt_secret() -> None:
    """Warn or fail at startup if the JWT secret is insecure.

    With the unset/default secret, anyone can forge tokens for any user.
    When KLANGK_PREVENT_INSECURE_JWT_SECRET is truthy, startup fails.
    Otherwise a warning is logged.
    """
    if jwt_secret_is_secure():
        return
    prevent = (
        resolve_env_secret("KLANGK_PREVENT_INSECURE_JWT_SECRET", "") or ""
    ).lower()
    if prevent in ("1", "true", "yes"):
        raise ConfigurationError(
            "KLANGK_JWT_SECRET is unset or the insecure default. Set a "
            "strong secret or remove KLANGK_PREVENT_INSECURE_JWT_SECRET."
        )
    logger.warning(
        "KLANGK_JWT_SECRET is unset or the insecure default. Set "
        "KLANGK_PREVENT_INSECURE_JWT_SECRET=1 in production."
    )


MIN_PASSWORD_LENGTH = int(
    resolve_env_secret("KLANGK_MIN_PASSWORD_LENGTH", "4")
)
MAX_PASSWORD_BYTES = 72  # bcrypt limit

# Set KLANGK_LOGIN_LOCKOUT_FAILURES=0 to disable login lockout.
LOGIN_LOCKOUT_FAILURES = int(
    resolve_env_secret("KLANGK_LOGIN_LOCKOUT_FAILURES", "0")
)

security = HTTPBearer(auto_error=False)


def validate_password_length(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Password must not exceed {MAX_PASSWORD_BYTES} bytes",
        )


def hash_password(password: str) -> str:
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password exceeds {MAX_PASSWORD_BYTES} bytes; "
            "call validate_password_length first"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(encoded, hashed.encode())


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def create_token(user_id: str, email: str) -> str:
    jti = str(uuid.uuid4())
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": user_id,
        "email": email,
        "jti": jti,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str, *, allow_expired: bool = False) -> dict:
    options = {"verify_exp": False} if allow_expired else {}
    return jwt.decode(
        token, SECRET_KEY, algorithms=[ALGORITHM], options=options
    )


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


def registration_enabled() -> bool:
    """Check if public registration is enabled."""
    val = resolve_env_secret("KLANGK_DISABLE_REGISTRATION", "")
    return val.lower() not in ("1", "true", "yes")


def invitations_enabled() -> bool:
    """Check if admin invitations are enabled."""
    val = resolve_env_secret("KLANGK_DISABLE_INVITES", "")
    return val.lower() not in ("1", "true", "yes")


async def register(
    req: RegisterRequest, verified: bool = False
) -> RegisterResult:
    if not registration_enabled():
        raise HTTPException(status_code=403, detail="Registration is disabled")
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    validate_email(req.email)
    validate_password_length(req.password)

    password_hash = hash_password(req.password)
    # The duplicate-email pre-check above is not atomic with the
    # INSERT, so two concurrent registrations can both pass it and
    # one hits the UNIQUE constraint. Catch that and return the same
    # opaque error as the pre-check (avoids user enumeration and an
    # unhandled HTTP 500).
    try:
        user = await model.create_user(
            req.email, password_hash, verified=verified
        )
    except SAIntegrityError:
        raise HTTPException(status_code=400, detail="Registration failed")
    token = None
    if verified:
        token = create_token(user["id"], user["email"])
    return RegisterResult(
        user_id=user["id"], email=user["email"], access_token=token
    )


async def login(req: LoginRequest) -> TokenResponse:
    # Check if locked out before doing any expensive work
    if LOGIN_LOCKOUT_FAILURES > 0:
        attempt_info = await model.get_login_attempt_info(req.email)
        is_locked, msg = _is_locked_out(attempt_info)
        if is_locked:
            raise HTTPException(status_code=429, detail=msg)

    user = await model.get_user_by_email(req.email)
    # OIDC-only users have no password hash; treat that as invalid
    # credentials rather than letting verify_password crash on None.
    if (
        user is None
        or not user.get("password_hash")
        or not verify_password(req.password, user["password_hash"])
    ):
        if LOGIN_LOCKOUT_FAILURES > 0:
            await model.record_failed_login(req.email)
            # Check if this attempt triggered a lockout
            updated_info = await model.get_login_attempt_info(req.email)
            if _should_lockout(updated_info):
                locked_until = datetime.now(timezone.utc) + timedelta(
                    seconds=LOGIN_LOCKOUT_DURATION
                )
                await model.set_login_lockout(
                    req.email, locked_until.isoformat()
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed attempts. Locked out for {LOGIN_LOCKOUT_DURATION // 60} minutes.",
                )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.get("verified"):
        raise HTTPException(
            status_code=403, detail="Account not verified. Check your email."
        )

    if LOGIN_LOCKOUT_FAILURES > 0:
        await model.clear_login_attempts(req.email)
    token = create_token(user["id"], user["email"])
    return TokenResponse(access_token=token)


async def refresh_token(token: str) -> TokenResponse:
    """Exchange a valid access token for a new one.

    The old token's JTI is blocklisted with the new token cached
    alongside it, making the endpoint idempotent: repeated calls
    with the same old token return the same new token.
    """
    jti = None
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        email = payload.get("email")
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not all([user_id, email, jti, exp]):  # pragma: no cover
            raise HTTPException(status_code=401, detail="Invalid token")

        if await model.is_token_blocklisted(jti):
            # Already refreshed — return the cached replacement
            cached = await model.get_refreshed_token(jti)
            if cached is not None:
                return TokenResponse(access_token=cached)
            raise HTTPException(
                status_code=401, detail="Token has been revoked"
            )

        user = await model.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")

        new_token = create_token(user_id, email)
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        await model.blocklist_token(jti, expires_at, new_token=new_token)
        return TokenResponse(access_token=new_token)

    except ExpiredSignatureError:
        # Token expired — check if it was previously refreshed
        payload = decode_token(token, allow_expired=True)
        jti = payload.get("jti")
        if jti:
            cached = await model.get_refreshed_token(jti)
            if cached is not None:
                return TokenResponse(access_token=cached)
        raise HTTPException(status_code=401, detail="Token expired")
    except JWTError:  # pragma: no cover
        raise HTTPException(status_code=401, detail="Invalid token")


VERIFY_TOKEN_EXPIRE_HOURS = 72
RESET_TOKEN_EXPIRE_HOURS = 1
INVITE_TOKEN_EXPIRE_HOURS = int(
    resolve_env_secret("KLANGK_INVITE_EXPIRE_HOURS", "72")
)


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


def create_invitation_token(invitation_id: str, email: str) -> str:
    """Create a JWT token for an invitation."""
    expire = datetime.now(timezone.utc) + timedelta(
        hours=INVITE_TOKEN_EXPIRE_HOURS
    )
    payload = {
        "sub": invitation_id,
        "email": email,
        "purpose": "invite",
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_invitation_token(token: str) -> tuple[str, str] | None:
    """Decode an invitation token. Returns (invitation_id, email) or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "invite":
            return None
        inv_id = payload.get("sub")
        email = payload.get("email")
        if not inv_id or not email:
            return None
        return (inv_id, email)
    except JWTError:
        return None


WORKSPACE_TOKEN_EXPIRE_HOURS = float(
    resolve_env_secret("KLANGK_WORKSPACE_TOKEN_HOURS", "24")
)


def create_workspace_token(workspace_id: str) -> str:
    """Create a JWT token identifying a workspace for container→host auth."""
    expire = datetime.now(timezone.utc) + timedelta(
        hours=WORKSPACE_TOKEN_EXPIRE_HOURS
    )
    payload = {"sub": workspace_id, "purpose": "workspace", "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# Sentinel returned by decode_workspace_token when the JWT is expired.
WORKSPACE_TOKEN_EXPIRED = "WORKSPACE_TOKEN_EXPIRED"


def decode_workspace_token(token: str) -> str | None:
    """Decode a workspace token.

    Returns:
        str workspace_id on success.
        WORKSPACE_TOKEN_EXPIRED if the token is expired.
        None for all other failures.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "workspace":
            return None
        return payload.get("sub")
    except ExpiredSignatureError:
        return WORKSPACE_TOKEN_EXPIRED
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

        if await model.is_token_blocklisted(jti):
            raise HTTPException(
                status_code=401, detail="Token has been revoked"
            )

        user = await model.get_user_by_id(user_id)
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
        payload = decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await model.is_token_blocklisted(jti):
            return None
        return await model.get_user_by_id(user_id)
    except JWTError:
        return None


# Sentinel returned by get_user_from_token when the JWT is expired.
TOKEN_EXPIRED = "TOKEN_EXPIRED"


async def get_user_from_token(token: str) -> dict | str | None:
    """Validate a token string (used for WebSocket auth).

    Returns:
        dict: the user record on success.
        TOKEN_EXPIRED: if the token signature is valid but expired.
        None: for all other failures (malformed, revoked, missing user).
    """
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await model.is_token_blocklisted(jti):
            return None
        return await model.get_user_by_id(user_id)
    except ExpiredSignatureError:
        return TOKEN_EXPIRED
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
            await model.blocklist_token(jti, expires_at)
    except JWTError:
        pass
