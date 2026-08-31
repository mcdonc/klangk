"""Authentication: password hashing, JWT tokens, login/register/refresh.

Stateful/config-reading auth lives on :class:`Auth`, an ``app.state``-owned
instance constructed once in :func:`build_app` (#1501, #1426). Every config
value is read from ``self.settings`` at call time — no module-level globals,
no import-time ``get_settings()``. Pure helpers (password hashing, email
validation, the lockout predicate, the Pydantic models) and the FastAPI
dependency callables (``get_current_user`` etc.) stay module-level.
"""

import asyncio
import base64
import functools
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .exceptions import ConfigurationError
from .settings import INSECURE_DEFAULT_SECRET

logger = logging.getLogger(__name__)

# Maximum password length in bytes. Originally bcrypt's 72-byte input
# limit; retained as policy now that hashing is PBKDF2 (#2576) so the
# password-requirement ceiling (settings._PASSWORD_REQUIRE_MAX) and the
# client-side mirrors of the length rule stay valid.
MAX_PASSWORD_BYTES = 72

# PBKDF2-HMAC-SHA512 parameters (#2576). hashlib.pbkdf2_hmac delegates to
# the OpenSSL the container provides, so under the FIPS provider (see
# #2570) password hashing routes through the validated module — unlike
# bcrypt, which bundles its own blowfish implementation and can never be
# FIPS-approvable. 600k iterations is deliberately above OWASP's current
# PBKDF2-HMAC-SHA512 recommendation (210k) for margin. Tests patch
# ``PBKDF2_ITERATIONS`` down for suite speed; the stored format embeds the
# iteration count, so hashes made under any value still verify.
#
# Timing-equalization coupling (#2618): ``dummy_verify_hash`` embeds the
# *current* value while stored hashes keep their creation-time count.
# Raising this constant therefore re-opens a small per-login timing gap
# for accounts hashed under the old count (their verifies stay cheaper
# than the dummy's until their password next changes). Re-baseline the
# dummy when bumping, or accept the gap for pre-bump accounts.
PBKDF2_ITERATIONS = 600_000
_HASH_SCHEME = "pbkdf2_sha512"
_SALT_BYTES = 16

# How often ``users.last_activity_at`` is re-stamped per user (#2588):
# authenticated API requests are frequent, but the inactivity window is
# measured in days — one UPDATE per user per minute is ample precision
# and keeps idle-poling clients off the write path.
ACTIVITY_STAMP_INTERVAL = 60.0
# Sanity ceiling on the iteration count read back from a stored hash, so
# a corrupt or tampered row cannot stall a login indefinitely.
_MAX_VERIFY_ITERATIONS = 10_000_000

# Display names for the character classes, in requirement-message order.
PASSWORD_CLASSES = (
    ("upper", "uppercase letter"),
    ("lower", "lowercase letter"),
    ("digit", "digit"),
    ("special", "special character"),
)


def password_class_counts(password: str) -> dict[str, int]:
    """ASCII character-class counts for the complexity policy (#2581).

    Classes are defined in ASCII terms — ``A-Z``, ``a-z``, ``0-9``, and
    everything else is "special" — so every non-ASCII character (``é``)
    counts as special, never as a letter or digit. This is deliberate:
    the Flutter UI's mirror (``PasswordPolicy``) and the CLI's mirror
    (``cli/account.password_complexity_error``) classify the same way,
    and Unicode's ``isupper``/``isdigit`` would accept things no operator
    means by "a digit" (``٣``) or "uppercase" (``Ⅰ``) while disagreeing
    with the clients. Server, web UI, and CLI must stay in sync.
    """
    upper = sum(1 for c in password if "A" <= c <= "Z")
    lower = sum(1 for c in password if "a" <= c <= "z")
    digit = sum(1 for c in password if "0" <= c <= "9")
    return {
        "upper": upper,
        "lower": lower,
        "digit": digit,
        "special": len(password) - upper - lower - digit,
    }


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


def ensure_not_disabled(user: dict) -> None:
    """Raise 403 when the account is disabled (#2588).

    Called at every session-minting and token-continuing choke point
    (login, OIDC callback, local login, verify/reset auto-login, token
    refresh, the HTTP ``get_current_user`` dependencies, and the
    WebSocket auth path). 403 — not 401 — so clients surface "Account
    disabled" instead of retrying a refresh/relogin loop that can never
    succeed.
    """
    if user.get("disabled"):
        raise HTTPException(status_code=403, detail="Account disabled")


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA512 (#2576).

    Returns a self-describing ``pbkdf2_sha512$<iterations>$<b64-salt>$
    <b64-hash>`` string, so ``verify_password`` needs no matching
    configuration to check it.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password exceeds {MAX_PASSWORD_BYTES} bytes; "
            "call validate_password_length first"
        )
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha512", encoded, salt, PBKDF2_ITERATIONS)
    return "$".join(
        (
            _HASH_SCHEME,
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a ``pbkdf2_sha512$...`` hash (#2576).

    Malformed input, an unknown scheme (a legacy bcrypt hash, say), or
    an out-of-range iteration count returns ``False`` rather than
    raising — callers treat garbage stored hashes as failed logins.
    """
    encoded = password.encode()
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        scheme, iterations_s, b64_salt, b64_digest = hashed.split("$")
        if scheme != _HASH_SCHEME:
            return False
        iterations = int(iterations_s)
        if not 1 <= iterations <= _MAX_VERIFY_ITERATIONS:
            return False
        salt = base64.b64decode(b64_salt, validate=True)
        expected = base64.b64decode(b64_digest, validate=True)
    except ValueError:
        # Wrong field count, non-integer iterations, or invalid base64
        # (binascii.Error subclasses ValueError).
        return False
    digest = hashlib.pbkdf2_hmac("sha512", encoded, salt, iterations)
    return hmac.compare_digest(digest, expected)


# Equalizes the cost of failing on an unknown (or OIDC-only) account with
# failing on a wrong password: login and resend-verification verify against
# this hash when there is no real one, so both paths burn one full password
# verify and response timing cannot enumerate accounts (#2618). The
# preimage is random per process, so no submitted password can ever match
# it — a match is still treated as invalid credentials by the callers.
# Computed lazily (and once) so importing the module costs no PBKDF2 work.
#
# Residual gap, accepted: a stored hash that is malformed or not the
# current scheme makes ``verify_password`` return False *before* any
# PBKDF2 work, so such an account fails faster than an unknown one. All
# deployments store current-scheme hashes (there are no legacy bcrypt
# rows), so the gap is unreachable today; see the note at
# ``PBKDF2_ITERATIONS`` for the bump-time coupling.
@functools.cache
def dummy_verify_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    identifier: str
    password: str


class EmailRequest(BaseModel):
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
        # Per-user monotonic clock of the last last_activity_at write
        # (#2588) — transient runtime state (not settings-derived), so
        # it survives reconfigure and is deliberately not reset there.
        self.activity_stamps: dict[str, float] = {}

    def reconfigure(self, app) -> None:
        self.app = app

    # --- settings-derived config (read live off app_state, #1608) ---

    @property
    def secret(self) -> str:
        return self.app.state.settings.jwt_secret

    @property
    def token_expire_hours(self) -> float:
        return self.app.state.settings.access_token_hours

    @property
    def min_password_length(self) -> int:
        return self.app.state.settings.min_password_length

    @property
    def password_require_upper(self) -> int:
        return int(self.app.state.settings.password_require_upper)

    @property
    def password_require_lower(self) -> int:
        return int(self.app.state.settings.password_require_lower)

    @property
    def password_require_digit(self) -> int:
        return int(self.app.state.settings.password_require_digit)

    @property
    def password_require_special(self) -> int:
        return int(self.app.state.settings.password_require_special)

    @property
    def password_history_count(self) -> int:
        return self.app.state.settings.password_history_count

    @property
    def password_requirements(self) -> dict:
        """Character-class counts a password must satisfy (#2581).

        Surfaced verbatim by ``/api/v1/config`` so clients can validate
        inline; ``0`` means "no requirement for this class".
        """
        return {
            "upper": self.password_require_upper,
            "lower": self.password_require_lower,
            "digit": self.password_require_digit,
            "special": self.password_require_special,
        }

    @property
    def login_lockout_failures(self) -> int:
        return self.app.state.settings.login_lockout_failures

    @property
    def max_sessions_per_user(self) -> int:
        """Concurrent-session cap per user (#2585). 0 = no limit."""
        return self.app.state.settings.max_sessions_per_user

    @property
    def login_lockout_duration(self) -> int:
        return self.app.state.settings.login_lockout_duration

    @property
    def login_lockout_window(self) -> int:
        return self.app.state.settings.login_lockout_window

    @property
    def invite_token_expire_hours(self) -> int:
        return self.app.state.settings.invite_expire_hours

    @property
    def workspace_token_expire_hours(self) -> float:
        return self.app.state.settings.workspace_token_hours

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
                "KLANGKD_JWT_SECRET is unset or the insecure default. Set a "
                "strong secret or remove KLANGKD_PREVENT_INSECURE_JWT_SECRET."
            )
        logger.warning(
            "KLANGKD_JWT_SECRET is unset or the insecure default. Set "
            "KLANGKD_PREVENT_INSECURE_JWT_SECRET=1 in production."
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

    def validate_password_complexity(self, password: str) -> None:
        """Enforce the configured character-class counts (#2581).

        Each class count (``KLANGKD_PASSWORD_REQUIRE_UPPER`` etc.) is the
        minimum number of characters of that class; 0 disables that class.
        Raises a single 400 listing every unmet requirement.
        """
        counts = password_class_counts(password)
        configured = {
            "upper": self.password_require_upper,
            "lower": self.password_require_lower,
            "digit": self.password_require_digit,
            "special": self.password_require_special,
        }
        unmet = [
            f"at least {need} {name}{'s' if need != 1 else ''}"
            for key, name in PASSWORD_CLASSES
            if (need := configured[key]) > 0 and counts[key] < need
        ]
        if unmet:
            raise HTTPException(
                status_code=400,
                detail=f"Password must contain {', '.join(unmet)}",
            )

    def validate_password(self, password: str) -> None:
        """Length + complexity — the one password gate every setter uses."""
        self.validate_password_length(password)
        self.validate_password_complexity(password)

    async def validate_password_not_reused(
        self, user_id: str, password: str
    ) -> None:
        """Reject *password* if it matches the current or a retired
        hash (#2582).

        Checks the current hash first (it lives in ``users``), then the
        retired hashes — up to ``password_history_count + 1`` PBKDF2
        verifies, run in a worker thread so the event loop is not
        blocked for the duration (#2611 review). No-op when
        ``password_history_count <= 0``.

        Known race, accepted: the check runs *before* the write, so two
        concurrent password sets on the same account can both validate
        against the pre-state (the loser's hash is then never retired).
        Every caller holds the current password, an admin session, or a
        reset token — the only exploiter is the account owner racing
        themselves — so the residual risk is a self-inflicted one-slot
        miss, not a bypass (#2611 review).
        """
        count = self.password_history_count
        if count <= 0:
            return
        users = self.app.state.model.users

        def _matches_any(hashes: list[str]) -> bool:
            return any(verify_password(password, h) for h in hashes)

        current = await users.get_password_hash(user_id)
        if current is not None and await asyncio.to_thread(
            _matches_any, [current]
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Password matches the current password; choose a"
                    " different one"
                ),
            )
        retired = await users.get_password_history(user_id, count)
        if await asyncio.to_thread(_matches_any, retired):
            raise HTTPException(
                status_code=400,
                detail=("Password was used recently; choose a different one"),
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

    # --- lockout accounting (shared by login and resend-verification) ---

    async def check_login_lockout(self, lockout_key: str) -> dict | None:
        """Raise 429 if *lockout_key* is currently locked out.

        Returns the pre-verify ``attempt_info`` (``None`` when lockout
        is disabled) for the caller to hand to
        :meth:`record_login_failure`, which needs it to apply the
        sliding-window reset.
        """
        if self.login_lockout_failures <= 0:
            return None
        attempt_info = (
            await self.app.state.model.login_attempts.get_login_attempt_info(
                lockout_key
            )
        )
        is_locked, msg = is_locked_out(attempt_info)
        if is_locked:
            raise HTTPException(status_code=429, detail=msg)
        return attempt_info

    async def record_login_failure(
        self, lockout_key: str, attempt_info: dict | None
    ) -> None:
        """Record a failed credential check against *lockout_key*.

        Applies the sliding-window reset (old failures stop counting
        toward the threshold) and raises 429 when this failure is the
        one that triggers the lockout.
        """
        if self.login_lockout_failures <= 0:
            return
        reset = self.window_elapsed(attempt_info)
        await self.app.state.model.login_attempts.record_failed_login(
            lockout_key, reset=reset
        )
        updated_info = (
            await self.app.state.model.login_attempts.get_login_attempt_info(
                lockout_key
            )
        )
        if self.should_lockout(updated_info):
            locked_until = datetime.now(timezone.utc) + timedelta(
                seconds=self.login_lockout_duration
            )
            await self.app.state.model.login_attempts.set_login_lockout(
                lockout_key, locked_until.isoformat()
            )
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Locked out for {self.login_lockout_duration // 60} minutes.",
            )

    async def clear_login_failures(self, lockout_key: str) -> None:
        """Clear failure counters after a successful credential check."""
        if self.login_lockout_failures > 0:
            await self.app.state.model.login_attempts.clear_login_attempts(
                lockout_key
            )

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

    async def issue_token(
        self,
        user_id: str,
        email: str,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> str:
        """Mint an access token AND register it as a session (#2585).

        Every interactive issuance path (password login, registration,
        email verification, password-reset auto-login, invite acceptance,
        OIDC callback, ``none``-mode local login) goes through here so the
        server-side session registry stays complete. The session row
        records the workstation it was established from (*source_ip* /
        *user_agent*, #2586) and :meth:`_audit_concurrent_logons`
        generates an audit record when the logon is concurrent with
        sessions from other workstations. Then
        :meth:`_enforce_session_limit` revokes the user's oldest sessions
        past ``KLANGKD_MAX_SESSIONS_PER_USER`` (0 = unlimited; the table
        is still purged of expired rows so it stays bounded).
        """
        token = self.create_token(user_id, email)
        payload = self.decode_token(token)
        expires_at = datetime.fromtimestamp(
            payload["exp"], tz=timezone.utc
        ).isoformat()
        await self.app.state.model.sessions.record_session(
            user_id,
            payload["jti"],
            expires_at,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        await self._audit_concurrent_logons(user_id, email, source_ip)
        await self._enforce_session_limit(user_id)
        return token

    async def _audit_concurrent_logons(
        self, user_id: str, email: str, source_ip: str | None
    ) -> None:
        """Audit concurrent logons from different workstations (#2586).

        A "workstation" is the effective client IP the session was
        established from. When this logon (from *source_ip*) is
        concurrent with an active session established from a different,
        known IP, an audit record is written to the server log — the
        signal an operator reviews to spot credentials shared with (or
        stolen by) a second machine. Sessions with an unknown IP
        (pre-#2586 rows, unresolvable clients) are never reported as
        different; expired rows are purged first so dead sessions
        don't generate records. Runs BEFORE the session limit so an
        about-to-be-evicted other-workstation session is still
        audited — that eviction is exactly the event of interest.
        """
        if source_ip is None:
            return
        sessions = self.app.state.model.sessions
        await sessions.purge_expired()
        rows = await sessions.list_sessions(user_id)
        others = sorted(
            {
                row["source_ip"]
                for row in rows
                if row["source_ip"] is not None
                and row["source_ip"] != source_ip
            }
        )
        if not others:
            return
        logger.info(
            "audit: concurrent logon from different workstations:"
            " user=%s email=%s new session from %s; concurrent with"
            " session(s) from %s",
            user_id,
            email,
            source_ip,
            ", ".join(others),
        )

    async def _enforce_session_limit(self, user_id: str) -> None:
        """Revoke the user's oldest sessions past the configured cap.

        Victims are removed oldest-first by blocklisting their JTIs (the
        same revocation path as logout: the next HTTP request 401s with
        "Token has been revoked"; the next WebSocket connect is rejected
        with 4001 → client logout), then their session rows are deleted.
        Blocklisting happens BEFORE the delete so a crash between the two
        can only leave a dead token's row behind (purged on the next
        issuance), never a live token without a row.
        """
        sessions = self.app.state.model.sessions
        # Dead sessions (their JWT already failed exp verification) never
        # count toward the cap; purge also bounds the table when the
        # limit is off.
        await sessions.purge_expired()
        limit = self.max_sessions_per_user
        if limit <= 0:
            return
        rows = await sessions.list_sessions(user_id)
        excess = rows[:-limit] if len(rows) > limit else []
        for row in excess:
            logger.info(
                "session limit: revoking oldest session jti=%s "
                "(user %s over cap %d)",
                row["jti"],
                user_id,
                limit,
            )
            await self.app.state.model.tokens.blocklist_token(
                row["jti"], row["expires_at"]
            )
        if excess:
            await sessions.remove_sessions([row["jti"] for row in excess])

    async def record_activity(self, user_id: str) -> None:
        """Stamp ``users.last_activity_at`` (throttled) on API access (#2588).

        Called from the token-auth choke points (the HTTP
        ``get_current_user`` dependencies and the WebSocket token path).
        At most one DB write per user per :data:`ACTIVITY_STAMP_INTERVAL`
        — the inactivity window is measured in days, so per-minute
        precision is ample and idle-polling clients stay off the write
        path.
        """
        now = time.monotonic()
        last = self.activity_stamps.get(user_id)
        if last is not None and now - last < ACTIVITY_STAMP_INTERVAL:
            return
        self.activity_stamps[user_id] = now
        await self.app.state.model.users.record_activity(user_id)

    def forget_user(self, user_id: str) -> None:
        """Drop the user's activity-throttle stamp (#2914).

        Called from the user-delete path so ``activity_stamps`` does not
        retain ids of deleted users for the process lifetime. A stale
        stamp only suppresses one throttled ``last_activity_at`` write,
        so a user deleted mid-interval loses nothing.
        """
        self.activity_stamps.pop(user_id, None)

    def decode_token(self, token: str, *, allow_expired: bool = False) -> dict:
        options = {"verify_exp": False} if allow_expired else {}
        return jwt.decode(
            token,
            self.secret,
            algorithms=[self.algorithm],
            options=options,
        )

    def _create_purpose_token(
        self,
        subject: str,
        purpose: str,
        expire_hours: int,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Create a signed JWT tagged with *purpose*, expiring in
        *expire_hours*, carrying *extra* claims."""
        expire = datetime.now(timezone.utc) + timedelta(hours=expire_hours)
        payload = {"sub": subject, "purpose": purpose, "exp": expire}
        if extra:
            payload.update(extra)
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    def _decode_purpose_token(self, token: str, purpose: str) -> dict | None:
        """Decode a JWT and require its ``purpose`` claim to match.

        Returns the payload, or None when the token is invalid, expired,
        or was minted for a different purpose.
        """
        try:
            payload = jwt.decode(
                token, self.secret, algorithms=[self.algorithm]
            )
        except JWTError:
            return None
        if payload.get("purpose") != purpose:
            return None
        return payload

    # --- email-verification tokens ---

    def create_verification_token(self, user_id: str) -> str:
        """Create a JWT token for email verification."""
        return self._create_purpose_token(
            user_id, "verify", self.verify_token_expire_hours
        )

    def decode_verification_token(self, token: str) -> str | None:
        """Decode a verification token. Returns user_id or None if invalid."""
        payload = self._decode_purpose_token(token, "verify")
        return payload.get("sub") if payload is not None else None

    # --- password-reset tokens ---

    def create_password_reset_token(self, user_id: str) -> str:
        """Create a JWT token for password reset."""
        return self._create_purpose_token(
            user_id, "reset", self.reset_token_expire_hours
        )

    def decode_password_reset_token(self, token: str) -> str | None:
        """Decode a password reset token. Returns user_id or None."""
        payload = self._decode_purpose_token(token, "reset")
        return payload.get("sub") if payload is not None else None

    # --- invitation tokens ---

    def create_invitation_token(self, invitation_id: str, email: str) -> str:
        """Create a JWT token for an invitation."""
        return self._create_purpose_token(
            invitation_id,
            "invite",
            self.invite_token_expire_hours,
            extra={"email": email},
        )

    def decode_invitation_token(self, token: str) -> tuple[str, str] | None:
        """Decode an invitation token. Returns (invitation_id, email) or None."""
        payload = self._decode_purpose_token(token, "invite")
        if payload is None:
            return None
        invitation_id = payload.get("sub")
        email = payload.get("email")
        if not invitation_id or not email:
            return None
        return (invitation_id, email)

    # --- workspace tokens ---

    def create_workspace_token(self, workspace_id: str) -> str:
        """Create a JWT token identifying a workspace for container→host auth."""
        return self._create_purpose_token(
            workspace_id, "workspace", self.workspace_token_expire_hours
        )

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
        self,
        req: RegisterRequest,
        verified: bool = False,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
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
        self.validate_password(req.password)

        password_hash = await asyncio.to_thread(hash_password, req.password)
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
            token = await self.issue_token(
                user["id"],
                user["email"],
                source_ip=source_ip,
                user_agent=user_agent,
            )
            await self.app.state.model.users.record_login(user["id"])
        return RegisterResult(
            user_id=user["id"], email=user["email"], access_token=token
        )

    async def login(
        self,
        req: LoginRequest,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        # Resolve the user by email or handle (#616).
        user = await self.app.state.model.users.get_user_by_identifier(
            req.identifier
        )
        # Key lockout accounting on the resolved user's canonical email so
        # handle and email attempts against the same account share one
        # counter. For an unresolved (nonexistent) identifier, fall back to
        # the raw input so brute-force on a made-up address is still
        # rate-limited.
        lockout_key = user["email"] if user else req.identifier

        # Check if locked out before doing any expensive work (the only
        # expensive step below is verify_password's PBKDF2, run in a
        # worker thread so the event loop is not blocked).
        attempt_info = await self.check_login_lockout(lockout_key)

        # OIDC-only users have no password hash; unknown users have no
        # account. Both verify against the dummy hash so the failure
        # path costs one full password verify either way — response
        # timing cannot enumerate accounts (#2618). Authorization
        # still requires a real hash to have matched. The dummy hash is
        # minted off the event loop too — the one-time PBKDF2 cost must
        # never block request handling (functools.cache makes later
        # calls free).
        password_hash = (user.get("password_hash") if user else None) or (
            await asyncio.to_thread(dummy_verify_hash)
        )
        password_ok = await asyncio.to_thread(
            verify_password, req.password, password_hash
        )
        if user is None or not user.get("password_hash") or not password_ok:
            await self.record_login_failure(lockout_key, attempt_info)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.get("verified"):
            raise HTTPException(
                status_code=403,
                detail="Account not verified. Check your email.",
            )
        # A disabled account has valid credentials but must not mint a
        # session (#2588) — admin re-enable is the only way back.
        ensure_not_disabled(user)

        await self.clear_login_failures(lockout_key)
        token = await self.issue_token(
            user["id"],
            user["email"],
            source_ip=source_ip,
            user_agent=user_agent,
        )
        # Stamp after minting, matching every other session-issuing site
        # (#2583): if minting fails, no login is recorded.
        await self.app.state.model.users.record_login(user["id"])
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
            if not all([user_id, email, jti, exp]):
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
            # A disabled account cannot rotate its way back in (#2588).
            ensure_not_disabled(user)
            # A refresh is authenticated API use (#2588 review): stamp so
            # a headless client that only refreshes (no other API calls)
            # still counts as active.
            await self.record_activity(user_id)

            new_token = self.create_token(user_id, email)
            expires_at = datetime.fromtimestamp(
                exp, tz=timezone.utc
            ).isoformat()
            logger.info(
                "REFRESH: blocklisting old access token; any WS still using "
                "it will be rejected as 4001 on its next connect"
            )
            await self.app.state.model.tokens.blocklist_token(
                jti, expires_at, new_token=new_token
            )
            # The refreshed JTI occupies the old session's slot (a refresh
            # is the same session under a new token, #2585): the user's
            # session count does not grow on refresh.
            new_payload = self.decode_token(new_token)
            new_expires_at = datetime.fromtimestamp(
                new_payload["exp"], tz=timezone.utc
            ).isoformat()
            await self.app.state.model.sessions.replace_session(
                jti, user_id, new_payload["jti"], new_expires_at
            )
            # Refreshing a pre-#2585 token (no row) INSERTS one; enforce
            # so the cap holds on every path that adds a session row,
            # not just logins (#2585 review).
            await self._enforce_session_limit(user_id)
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
        except JWTError:
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
                logger.info(
                    "token reject: BLOCKLISTED (revoked by a refresh or "
                    "logout -> WS will close 4001 -> client logout)"
                )
                return None
            user = await self.app.state.model.users.get_user_by_id(user_id)
            if user is None:
                return None
            # Disabled accounts keep their WS connections shut (#2588):
            # returning None rejects the connect like any dead token.
            if user.get("disabled"):
                logger.info(
                    "token reject: ACCOUNT DISABLED -> WS will close 4001"
                    " -> client logout"
                )
                return None
            await self.record_activity(user_id)
            return user
        except ExpiredSignatureError:
            logger.info(
                "token reject: EXPIRED -> WS will close 4002 -> client logout"
            )
            return self.TOKEN_EXPIRED
        except JWTError:
            return None

    async def logout(self, token: str) -> None:
        """Blocklist the token's JTI and drop its session row."""
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
                await self.app.state.model.sessions.remove_session(jti)
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
        # A disabled account fails every authenticated request (#2588);
        # 403 (not 401) so clients don't loop on refresh/relogin.
        ensure_not_disabled(user)
        await auth.record_activity(user_id)
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Like get_current_user but returns None instead of raising 401.

    Exception: a disabled account raises 403 (#2588) — returning None
    would silently degrade the endpoint to its anonymous view and hide
    the reason from the client.
    """
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
        user = await request.app.state.model.users.get_user_by_id(user_id)
        if user is None:
            return None
        # Valid credentials on a disabled account still 403 (#2588) —
        # returning None here would silently degrade /config to the
        # anonymous view and hide the reason from the client.
        ensure_not_disabled(user)
        await auth.record_activity(user_id)
        return user
    except JWTError:
        return None


async def get_current_user_lenient(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict | None:
    """Like get_current_user_optional but never raises — tolerates revoked
    tokens and disabled accounts.

    Used by logout (#2687), which must return 200 for a token in any
    state: a disabled account logging out is a client cleaning up after
    itself, not an auth attempt, so the #2588 403 does not apply. Does
    not record activity (the session is ending) and does not check the
    blocklist (the token is about to be blocklisted anyway; a repeat
    logout still resolving its user only affects the optional OIDC
    redirect URL).
    """
    if credentials is None:
        return None
    auth = request.app.state.auth
    try:
        payload = auth.decode_token(credentials.credentials)
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return await request.app.state.model.users.get_user_by_id(user_id)
    except JWTError:
        return None
