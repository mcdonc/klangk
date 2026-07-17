"""Authentication: password hashing, JWT tokens, login/register/refresh.

Stateful/config-reading auth lives on :class:`Auth`, an ``app.state``-owned
instance constructed once in :func:`build_app` (#1501, #1426). Every config
value is read from ``self.settings`` at call time — no module-level globals,
no import-time ``get_settings()``. Pure helpers (password hashing, email
validation, the lockout predicate, the Pydantic models) and the FastAPI
dependency callables (``get_current_user`` etc.) stay module-level.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .exceptions import ConfigurationError
from .settings import INSECURE_DEFAULT_SECRET

logger = logging.getLogger(__name__)

# Maximum password length bcrypt will accept (its 72-byte limit).
MAX_PASSWORD_BYTES = 72

security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Pure helpers (no config) — module-level
# ---------------------------------------------------------------------------


def is_locked_out(
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
    # Named ``email`` on the wire for client back-compat (frontend/CLI
    # both POST this field), but accepts an email *or* a handle —
    # ``Auth.login`` resolves it via ``get_user_by_identifier`` (#616).
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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


# ---------------------------------------------------------------------------
# Auth instance — config-reading, app.state-owned (#1501, #1426)
# ---------------------------------------------------------------------------


class Auth:
    """Owns every auth config value and JWT operation.

    Constructed once in :func:`build_app` and stored on ``app.state.auth``.
    Reads ``self.settings`` at construction for the resolved config (all
    ``file:``/``cmd:`` values already resolved, #1461) and at call time for
    the toggle-style fields. Token create/decode close over ``self.secret``
    / ``self.algorithm`` so every caller agrees on one key.
    """

    # Email-verification / password-reset token lifetimes are fixed
    # policy (not env-driven). Reached as instance attrs so every token
    # lifetime reads uniformly: auth.<kind>_expire_hours.

    # Sentinels returned by the decode helpers.
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    WORKSPACE_TOKEN_EXPIRED = "WORKSPACE_TOKEN_EXPIRED"

    def __init__(self, app):
        self.app = app
        self.algorithm = "HS256"
        # Fixed-policy token lifetimes (not env-driven).
        self.verify_token_expire_hours = 72
        self.reset_token_expire_hours = 1

    def reconfigure(self, app) -> None:
        self.app = app

    # --- settings-derived config (read live off app_state, #1608) ---

    @property
    def secret(self) -> str:
        return self.app.state.settings.jwt_secret

    @property
    def token_expire_hours(self) -> float:
        return float(self.app.state.settings.access_token_hours)

    @property
    def min_password_length(self) -> int:
        return int(self.app.state.settings.min_password_length)

    @property
    def login_lockout_failures(self) -> int:
        return int(self.app.state.settings.login_lockout_failures)

    @property
    def login_lockout_duration(self) -> int:
        return int(self.app.state.settings.login_lockout_duration)

    @property
    def login_lockout_window(self) -> int:
        return int(self.app.state.settings.login_lockout_window)

    @property
    def invite_token_expire_hours(self) -> int:
        return int(self.app.state.settings.invite_expire_hours)

    @property
    def workspace_token_expire_hours(self) -> float:
        return float(self.app.state.settings.workspace_token_hours)

    # --- secret / startup guard ---

    def jwt_secret_is_secure(self) -> bool:
        """True if a non-empty, non-default JWT signing secret is configured."""
        return bool(self.secret) and self.secret != INSECURE_DEFAULT_SECRET

    def require_secure_jwt_secret(self) -> None:
        """Warn or fail at startup if the JWT secret is insecure.

        With the unset/default secret, anyone can forge tokens for any user.
        When ``prevent_insecure_jwt_secret`` is truthy, startup fails.
        Otherwise a warning is logged.
        """
        if self.jwt_secret_is_secure():
            return
        prevent = self.app.state.settings.prevent_insecure_jwt_secret.lower()
        if prevent in ("1", "true", "yes"):
            raise ConfigurationError(
                "KLANGK_JWT_SECRET is unset or the insecure default. Set a "
                "strong secret or remove KLANGK_PREVENT_INSECURE_JWT_SECRET."
            )
        logger.warning(
            "KLANGK_JWT_SECRET is unset or the insecure default. Set "
            "KLANGK_PREVENT_INSECURE_JWT_SECRET=1 in production."
        )

    # --- toggles ---

    def registration_enabled(self) -> bool:
        """Check if public registration is enabled."""
        val = self.app.state.settings.disable_registration
        return val.lower() not in ("1", "true", "yes")

    def invitations_enabled(self) -> bool:
        """Check if admin invitations are enabled."""
        val = self.app.state.settings.disable_invites
        return val.lower() not in ("1", "true", "yes")

    def validate_password_length(self, password: str) -> None:
        if len(password) < self.min_password_length:
            raise HTTPException(
                status_code=400,
                detail=f"Password must be at least {self.min_password_length} characters",
            )
        if len(password.encode()) > MAX_PASSWORD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Password must not exceed {MAX_PASSWORD_BYTES} bytes",
            )

    # --- lockout predicates (read lockout config) ---

    def should_lockout(self, attempt_info: dict | None) -> bool:
        """Return True if the attempt count exceeds the threshold."""
        if attempt_info is None:
            return False
        return (
            attempt_info.get("attempt_count", 0) >= self.login_lockout_failures
        )

    def window_elapsed(self, attempt_info: dict | None) -> bool:
        """Return True if the first failure in *attempt_info* predates the
        sliding lockout window.

        Used to decide whether ``record_failed_login`` should reset the
        count (old failures stop counting) rather than increment.  ``None``
        info, a missing/unparseable ``first_attempt_at`` → not elapsed.
        """
        if attempt_info is None:
            return False
        first = attempt_info.get("first_attempt_at")
        if not first:
            return False
        try:
            first_dt = datetime.fromisoformat(first)
        except (TypeError, ValueError):
            return False
        return (
            datetime.now(timezone.utc) - first_dt
        ).total_seconds() > self.login_lockout_window

    # --- access tokens ---

    def create_token(self, user_id: str, email: str) -> str:
        jti = str(uuid.uuid4())
        expire = datetime.now(timezone.utc) + timedelta(
            hours=self.token_expire_hours
        )
        payload = {
            "sub": user_id,
            "email": email,
            "jti": jti,
            "exp": expire,
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_token(self, token: str, *, allow_expired: bool = False) -> dict:
        options = {"verify_exp": False} if allow_expired else {}
        return jwt.decode(
            token,
            self.secret,
            algorithms=[self.algorithm],
            options=options,
        )

    # --- email-verification tokens ---

    def create_verification_token(self, user_id: str) -> str:
        """Create a JWT token for email verification."""
        expire = datetime.now(timezone.utc) + timedelta(
            hours=self.verify_token_expire_hours
        )
        payload = {"sub": user_id, "purpose": "verify", "exp": expire}
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_verification_token(self, token: str) -> str | None:
        """Decode a verification token. Returns user_id or None if invalid."""
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm]
            )
            if payload.get("purpose") != "verify":
                return None
            return payload.get("sub")
        except JWTError:
            return None

    # --- password-reset tokens ---

    def create_password_reset_token(self, user_id: str) -> str:
        """Create a JWT token for password reset."""
        expire = datetime.now(timezone.utc) + timedelta(
            hours=self.reset_token_expire_hours
        )
        payload = {"sub": user_id, "purpose": "reset", "exp": expire}
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_password_reset_token(self, token: str) -> str | None:
        """Decode a password reset token. Returns user_id or None."""
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm]
            )
            if payload.get("purpose") != "reset":
                return None
            return payload.get("sub")
        except JWTError:
            return None

    # --- invitation tokens ---

    def create_invitation_token(self, invitation_id: str, email: str) -> str:
        """Create a JWT token for an invitation."""
        expire = datetime.now(timezone.utc) + timedelta(
            hours=self.invite_token_expire_hours
        )
        payload = {
            "sub": invitation_id,
            "email": email,
            "purpose": "invite",
            "exp": expire,
        }
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_invitation_token(self, token: str) -> tuple[str, str] | None:
        """Decode an invitation token. Returns (invitation_id, email) or None."""
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm]
            )
            if payload.get("purpose") != "invite":
                return None
            inv_id = payload.get("sub")
            email = payload.get("email")
            if not inv_id or not email:
                return None
            return (inv_id, email)
        except JWTError:
            return None

    # --- workspace tokens ---

    def create_workspace_token(self, workspace_id: str) -> str:
        """Create a JWT token identifying a workspace for container→host auth."""
        expire = datetime.now(timezone.utc) + timedelta(
            hours=self.workspace_token_expire_hours
        )
        payload = {"sub": workspace_id, "purpose": "workspace", "exp": expire}
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def decode_workspace_token(self, token: str) -> str | None:
        """Decode a workspace token.

        Returns:
            str workspace_id on success.
            WORKSPACE_TOKEN_EXPIRED if the token is expired.
            None for all other failures.
        """
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm]
            )
            if payload.get("purpose") != "workspace":
                return None
            return payload.get("sub")
        except ExpiredSignatureError:
            return self.WORKSPACE_TOKEN_EXPIRED
        except JWTError:
            return None

    # --- registration / login flows ---

    async def register(
        self, req: RegisterRequest, verified: bool = False
    ) -> RegisterResult:
        if not self.registration_enabled():
            raise HTTPException(
                status_code=403, detail="Registration is disabled"
            )
        existing = await self.app.state.model.users.get_user_by_email(
            req.email
        )
        if existing is not None:
            raise HTTPException(status_code=400, detail="Registration failed")
        validate_email(req.email)
        self.validate_password_length(req.password)

        password_hash = hash_password(req.password)
        # The duplicate-email pre-check above is not atomic with the
        # INSERT, so two concurrent registrations can both pass it and
        # one hits the UNIQUE constraint. Catch that and return the same
        # opaque error as the pre-check (avoids user enumeration and an
        # unhandled HTTP 500).
        try:
            user = await self.app.state.model.users.create_user(
                req.email, password_hash, verified=verified
            )
        except SAIntegrityError:
            raise HTTPException(status_code=400, detail="Registration failed")
        token = None
        if verified:
            token = self.create_token(user["id"], user["email"])
        return RegisterResult(
            user_id=user["id"], email=user["email"], access_token=token
        )

    async def login(self, req: LoginRequest) -> TokenResponse:
        # Resolve the user by email or handle (#616): the wire field is
        # named ``email`` for back-compat but accepts a handle too.
        user = await self.app.state.model.users.get_user_by_identifier(
            req.email
        )
        # Key lockout accounting on the resolved user's canonical email so
        # handle and email attempts against the same account share one
        # counter. For an unresolved (nonexistent) identifier, fall back to
        # the raw input so brute-force on a made-up address is still
        # rate-limited.
        lockout_key = user["email"] if user else req.email

        # Check if locked out before doing any expensive work (the only
        # expensive step below is verify_password's bcrypt).
        if self.login_lockout_failures > 0:
            attempt_info = await self.app.state.model.login_attempts.get_login_attempt_info(
                lockout_key
            )
            is_locked, msg = is_locked_out(attempt_info)
            if is_locked:
                raise HTTPException(status_code=429, detail=msg)

        # OIDC-only users have no password hash; treat that as invalid
        # credentials rather than letting verify_password crash on None.
        if (
            user is None
            or not user.get("password_hash")
            or not verify_password(req.password, user["password_hash"])
        ):
            if self.login_lockout_failures > 0:
                # Reuse the attempt_info fetched up front for the lockout
                # check to decide whether the sliding window has elapsed —
                # if so, reset the count instead of incrementing, so old
                # failures stop counting toward the threshold.
                reset = self.window_elapsed(attempt_info)
                await self.app.state.model.login_attempts.record_failed_login(
                    lockout_key, reset=reset
                )
                # Check if this attempt triggered a lockout
                updated_info = await self.app.state.model.login_attempts.get_login_attempt_info(
                    lockout_key
                )
                if self.should_lockout(updated_info):
                    locked_until = datetime.now(timezone.utc) + timedelta(
                        seconds=self.login_lockout_duration
                    )
                    await (
                        self.app.state.model.login_attempts.set_login_lockout(
                            lockout_key, locked_until.isoformat()
                        )
                    )
                    raise HTTPException(
                        status_code=429,
                        detail=f"Too many failed attempts. Locked out for {self.login_lockout_duration // 60} minutes.",
                    )
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.get("verified"):
            raise HTTPException(
                status_code=403,
                detail="Account not verified. Check your email.",
            )

        if self.login_lockout_failures > 0:
            await self.app.state.model.login_attempts.clear_login_attempts(
                lockout_key
            )
        token = self.create_token(user["id"], user["email"])
        return TokenResponse(access_token=token)

    async def refresh_token(self, token: str) -> TokenResponse:
        """Exchange a valid access token for a new one.

        The old token's JTI is blocklisted with the new token cached
        alongside it, making the endpoint idempotent: repeated calls
        with the same old token return the same new token.
        """
        jti = None
        try:
            payload = self.decode_token(token)
            user_id = payload.get("sub")
            email = payload.get("email")
            jti = payload.get("jti")
            exp = payload.get("exp")
            if not all([user_id, email, jti, exp]):  # pragma: no cover
                raise HTTPException(status_code=401, detail="Invalid token")

            if await self.app.state.model.tokens.is_token_blocklisted(jti):
                # Already refreshed — return the cached replacement
                cached = await self.app.state.model.tokens.get_refreshed_token(
                    jti
                )
                if cached is not None:
                    return TokenResponse(access_token=cached)
                raise HTTPException(
                    status_code=401, detail="Token has been revoked"
                )

            user = await self.app.state.model.users.get_user_by_id(user_id)
            if user is None:
                raise HTTPException(status_code=401, detail="User not found")

            new_token = self.create_token(user_id, email)
            expires_at = datetime.fromtimestamp(
                exp, tz=timezone.utc
            ).isoformat()
            await self.app.state.model.tokens.blocklist_token(
                jti, expires_at, new_token=new_token
            )
            return TokenResponse(access_token=new_token)

        except ExpiredSignatureError:
            # Token expired — check if it was previously refreshed
            payload = self.decode_token(token, allow_expired=True)
            jti = payload.get("jti")
            if jti:
                cached = await self.app.state.model.tokens.get_refreshed_token(
                    jti
                )
                if cached is not None:
                    return TokenResponse(access_token=cached)
            raise HTTPException(status_code=401, detail="Token expired")
        except JWTError:  # pragma: no cover
            raise HTTPException(status_code=401, detail="Invalid token")

    async def get_user_from_token(self, token: str) -> dict | str | None:
        """Validate a token string (used for WebSocket auth).

        Returns:
            dict: the user record on success.
            TOKEN_EXPIRED: if the token signature is valid but expired.
            None: for all other failures (malformed, revoked, missing user).
        """
        try:
            payload = self.decode_token(token)
            user_id = payload.get("sub")
            jti = payload.get("jti")
            if user_id is None or jti is None:
                return None
            if await self.app.state.model.tokens.is_token_blocklisted(jti):
                return None
            return await self.app.state.model.users.get_user_by_id(user_id)
        except ExpiredSignatureError:
            return self.TOKEN_EXPIRED
        except JWTError:
            return None

    async def logout(self, token: str) -> None:
        """Blocklist the token's JTI."""
        try:
            payload = self.decode_token(token)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                expires_at = datetime.fromtimestamp(
                    exp, tz=timezone.utc
                ).isoformat()
                await self.app.state.model.tokens.blocklist_token(
                    jti, expires_at
                )
        except JWTError:
            pass


# ---------------------------------------------------------------------------
# FastAPI dependency callables — module-level, reach app.state.auth
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    auth = request.app.state.auth
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = auth.decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        if await request.app.state.model.tokens.is_token_blocklisted(jti):
            raise HTTPException(
                status_code=401, detail="Token has been revoked"
            )

        user = await request.app.state.model.users.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Like get_current_user but returns None instead of raising 401."""
    if credentials is None:
        return None
    auth = request.app.state.auth
    try:
        payload = auth.decode_token(credentials.credentials)
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if user_id is None or jti is None:
            return None
        if await request.app.state.model.tokens.is_token_blocklisted(jti):
            return None
        return await request.app.state.model.users.get_user_by_id(user_id)
    except JWTError:
        return None
