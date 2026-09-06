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
import ipaddress
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
from . import dpop as dpop_mod
from .model.users import parse_user_ts
from .settings import INSECURE_DEFAULT_SECRET

logger = logging.getLogger(__name__)

# Maximum password length in bytes. Originally bcrypt's 72-byte input
# limit; retained as policy now that hashing is PBKDF2 (#2576) so the
# password-requirement ceiling (settings._PASSWORD_REQUIRE_MAX) and the
# client-side mirrors of the length rule stay valid.
MAX_PASSWORD_BYTES = 72

# Longest attempted identifier stored in an audit row (#3205): bounds
# the size of attacker-controlled text a ``login.failed`` detail can
# carry (row count is bounded separately by the per-class prune cap).
AUDIT_IDENTIFIER_MAX = 256

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

# How often the last-seen stamp throttle key dict is length-capped
# sessions churn (every refresh mints a new JTI), so the dict is
# length-capped the same way the rate-limit dicts are — insertion order
# under a monotonic clock tracks first-stamp order, so the head is a
# reasonable eviction victim (it may occasionally be a hot live entry
# whose key never moved; the cost is one redundant DB write later).
SESSION_STAMP_MAX_ENTRIES = 10_000
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


def password_expired_error() -> HTTPException:
    """The machine-readable "password expired" 403 (#3177).

    Clients (CLI / TUI) detect the state via ``detail["error"] ==
    "password_expired"`` and route to the set-new-password flow instead
    of showing a bare failure.
    """
    return HTTPException(
        status_code=403,
        detail={
            "error": "password_expired",
            "message": (
                "Password has expired and must be changed before you"
                " can sign in"
            ),
        },
    )


def _count_class(password: str, lo: str, hi: str) -> int:
    """Count the ASCII characters in the inclusive range ``lo``..``hi``."""
    return sum(1 for c in password if lo <= c <= hi)


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
    upper = _count_class(password, "A", "Z")
    lower = _count_class(password, "a", "z")
    digit = _count_class(password, "0", "9")
    return {
        "upper": upper,
        "lower": lower,
        "digit": digit,
        "special": len(password) - upper - lower - digit,
    }


security = HTTPBearer(auto_error=False)

#: The request header marking a session-minting request as coming from
#: the web SPA (#3230). Sessions minted for the web client carry a DPoP
#: bind deadline (the ``wbd`` claim): if the session is not bound to a
#: key within the grace window, every later use is refused — a script
#: in the page can read the unbound JWT and sabotage the bind calls,
#: but the exfiltrated token dies at the deadline. CLI/TUI requests send
#: no such header and stay unbound indefinitely by design. The header
#: cannot be abused to loosen anyone's session: it only tightens the
#: sender's own session's requirements.
WEB_CLIENT_HEADER = "klangk-web-client"

#: The request header carrying the SPA's public DPoP binding JWK on a
#: minting request (#3230): base64url of the compact JSON ``{kty, crv,
#: x, y}``. A marked mint carries this so the token is **born bound**
#: (``cnf.jkt`` from the first byte) — there is no unbound window for a
#: page script to read, sabotage, or bind-first with its own key. The
#: header value is validated server-side exactly like ``POST
#: /auth/bind``'s body JWK.
BINDING_JWK_HEADER = "klangk-binding-jwk"

#: The JWT claim carrying a web-minted session's DPoP bind deadline as
#: a Unix-epoch float (#3230) — mint time plus
#: ``KLANGKD_WEB_BIND_GRACE_SECONDS``. Carried unchanged across refresh
#: and bind swaps so a rotation can never reset it.
BIND_DEADLINE_CLAIM = "wbd"


# ---------------------------------------------------------------------------
# Pure helpers (no config) — module-level
# ---------------------------------------------------------------------------


def password_edit_distance(old: str, new: str) -> int:
    """Levenshtein distance between two passwords, in code points (#3173).

    Substitutions, insertions, and deletions each count as one changed
    character. Insertions/deletions counting is what kills
    the positional-diff workaround (prepending to ``Password1234!``
    changes every position yet is one inserted character). Passwords
    are capped at 72 bytes (so at most 72 code points), keeping
    the DP table trivially small. Pure Python keeps the server/CLI
    mirrors identical.
    """
    prev = list(range(len(new) + 1))
    for i, old_char in enumerate(old, start=1):
        cur = [i] + [0] * len(new)
        for j, new_char in enumerate(new, start=1):
            cur[j] = min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (old_char != new_char),
            )
        prev = cur
    return prev[-1]


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


def _unmet_requirement(
    key: str, name: str, configured: dict, counts: dict
) -> str | None:
    """The human phrase for an unmet class requirement, or ``None``
    when the class requirement is met (or its minimum is 0 —
    disabled)."""
    need = configured[key]
    if need > 0 and counts[key] < need:
        return f"at least {need} {name}{'s' if need != 1 else ''}"
    return None


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


def ensure_password_changed(user: dict) -> None:
    """Raise 403 when the account requires a password change (#3172).

    Called by ``get_current_user`` on every authenticated request.
    The change-password endpoint is exempt so the user can actually
    clear the flag. 403 with a machine-readable detail so clients
    can drive the forced-change flow.
    """
    if user.get("must_change_password"):
        raise HTTPException(
            status_code=403,
            detail="Password change required",
        )


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


async def verify_login_password(user: dict | None, password: str) -> bool:
    """Time-equalized password verify for login-style endpoints (#2618).

    OIDC-only users have no password hash; unknown users have no
    account. Both verify against the dummy hash so the failure path
    costs one full password verify either way — response timing cannot
    enumerate accounts. The dummy hash is minted off the event loop
    too — the one-time PBKDF2 cost must never block request handling
    (functools.cache makes later calls free). Authorization still
    requires a real hash to have matched.
    """
    password_hash = (user.get("password_hash") if user else None) or (
        await asyncio.to_thread(dummy_verify_hash)
    )
    return await asyncio.to_thread(verify_password, password, password_hash)


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    identifier: str
    password: str


class BindRequest(BaseModel):
    """DPoP key registration (#3218): a public EC P-256 JWK."""

    jwk: dict


class ChangeExpiredPasswordRequest(BaseModel):
    """Expired-password rotation: the current (expired) password plus
    its replacement (#3177). The current password is the ownership
    proof, exactly as at login."""

    identifier: str
    current_password: str
    new_password: str


class EmailRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


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


def _other_workstation_ips(rows: list[dict], source_ip: str) -> list[str]:
    """The distinct known IPs (other than *source_ip*) the user has
    active sessions from, sorted.

    Sessions with an unknown IP (pre-#2586 rows, unresolvable clients)
    are never reported as different."""
    return sorted(
        {
            row["source_ip"]
            for row in rows
            if row["source_ip"] is not None and row["source_ip"] != source_ip
        }
    )


def _unwrap_mapped(addr):
    """The IPv4 form of an IPv4-mapped IPv6 address
    (``::ffff:1.2.3.4`` → ``1.2.3.4``), or the address unchanged.

    Without this, two mapped-form IPv4 addresses both parse as IPv6
    whose /64 is ``::`` — collapsing every IPv4 client into one
    "workstation" under proxies that forward the raw mapped peer.
    """
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _same_ipv6_network(rec, pres) -> bool:
    """True when two (already-unwrapped) addresses are IPv6 hosts
    inside one /64 — the roaming-tolerant same-workstation judgement."""
    if rec.version != 6 or pres.version != 6:
        return False
    return pres in ipaddress.ip_network(f"{rec}/64", strict=False)


def same_workstation_ip(recorded: str, presented: str) -> bool:
    """True when two known client IPs name the same workstation (#3194).

    Exact equality, or two IPv6 addresses inside one /64 — hosts on
    the same IPv6 link rotate addresses inside the prefix (privacy
    extensions), and byte-equality would kill a roaming client's
    session on every rotation. Everything else (different IPv4s, an
    IPv4 vs an IPv6, unparseable strings that differ) is different.
    Note the #2586 concurrent-logon audit deliberately compares raw
    strings instead (strictst reading); binding is the softer,
    roaming-tolerant judgement.
    """
    if recorded == presented:
        return True
    try:
        rec = _unwrap_mapped(ipaddress.ip_address(recorded))
        pres = _unwrap_mapped(ipaddress.ip_address(presented))
    except ValueError:
        return False
    if rec == pres:
        return True
    return _same_ipv6_network(rec, pres)


def _known_ips_differ(rec_ip: str | None, ip: str | None) -> bool:
    """True when two KNOWN addresses name different networks; an
    unknown on either side is never different (fail-open, #3194)."""
    if rec_ip is None or ip is None:
        return False
    return not same_workstation_ip(rec_ip, ip)


def _known_agents_differ(rec_agent: str | None, agent: str | None) -> bool:
    """True when two KNOWN user agents differ; an unknown on either
    side is never different (fail-open, #3194)."""
    if rec_agent is None or agent is None:
        return False
    return rec_agent != agent


def workstation_mismatch(
    recorded: tuple[str | None, str | None],
    presented: tuple[str | None, str | None],
    strict: bool,
) -> bool:
    """True when *presented* differs from the recorded *workstation*
    (#3194).

    Each tuple is ``(source_ip, user_agent)``. Unknown values
    (``None`` on either side) are never a mismatch — the same
    fail-open posture as the concurrent-logon audit (#2586): a
    pre-#2586 session row or an unresolvable client cannot be judged,
    so it is never rejected. In ``strict`` mode a known-but-different
    user agent is also a mismatch.
    """
    if _known_ips_differ(recorded[0], presented[0]):
        return True
    return strict and _known_agents_differ(recorded[1], presented[1])


def _request_workstation(
    request: Request | None,
) -> tuple[str | None, str | None]:
    """The ``(ip, user_agent)`` an HTTP request presents (#3194); a
    request-less call resolves to unknown (fail-open).

    Reached only when binding is armed (``reject_replayed_session``
    short-circuits first), so minimal test app states without a
    ``util`` stay on the unarmed path.
    """
    if request is None:
        return None, None
    util = request.app.state.util
    host = request.client.host if request.client else None
    return util.workstation(request.headers, host)


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
        # One-time OIDC login codes (#3201): code -> (access_token,
        # email, exp). The callback redirects a single-use code (not the
        # session JWT) to the CLI/web completer, which redeems it via
        # POST /auth/oidc/exchange. Process-local like the rate-limit
        # dicts: redemption pops the entry, and anything surviving a
        # restart is already expired.
        self.pending_login_codes: dict[str, tuple[str, str, float]] = {}
        self.login_code_ttl_seconds = 60
        # Per-user monotonic clock of the last last_activity_at write
        # (#2588) — transient runtime state (not settings-derived), so
        # it survives reconfigure and is deliberately not reset there.
        self.activity_stamps: dict[str, float] = {}
        # Per-JTI monotonic clock of the last session last_seen_at
        # write (#3151) — same class of transient throttle state, keyed
        # by session so one user's two browsers cannot suppress each
        # other's stamps.
        self.session_stamps: dict[str, float] = {}
        # DPoP proof-JTI → expiry map (#3218) — the replay half of
        # proof verification. Transient runtime state like the stamp
        # dicts above. A restart clears it, which can re-admit one
        # replay of an already-consumed proof inside its freshness
        # window — that also requires the token itself, so it rides
        # the same boat as the WS token-in-URL problem (#3201).
        self.dpop_replay: dict[str, float] = {}

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
    def password_min_changed(self) -> int:
        """Min character edit distance for self-service changes (#3173)."""
        return self.app.state.settings.password_min_changed

    @property
    def password_min_age_hours(self) -> int:
        """Minimum password age in hours (#3177).

        0 (default) disables the check. Read live off settings so a
        SIGHUP reload applies without a restart.
        """
        return self.app.state.settings.password_min_age_hours

    @property
    def password_max_age_days(self) -> int:
        """Maximum password age in days (#3177).

        0 (default) disables expiry. Read live off settings so a SIGHUP
        reload applies without a restart.
        """
        return self.app.state.settings.password_max_age_days

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

    @property
    def session_idle_timeout_minutes(self) -> int:
        """Configured idle-session window in minutes; 0 = off (#3151)."""
        return self.app.state.settings.session_idle_timeout_minutes

    @property
    def privileged_session_idle_timeout_minutes(self) -> int:
        """The privileged (admins-group) window in minutes; 0 = no
        privileged split — admins use the general window (#3151)."""
        return self.app.state.settings.privileged_session_idle_timeout_minutes

    @property
    def web_bind_grace_seconds(self) -> int:
        """How long a web-minted session may stay unbound, in seconds;
        0 = no deadline (#3230)."""
        return self.app.state.settings.web_bind_grace_seconds

    @property
    def session_workstation_binding(self) -> str:
        """The session-binding mode: off | ip | strict (#3194).

        Normalized at construction by the settings validator; read
        live off settings so a SIGHUP reload arms or disarms binding
        without a restart.
        """
        return self.app.state.settings.session_workstation_binding

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
            phrase
            for key, name in PASSWORD_CLASSES
            if (phrase := _unmet_requirement(key, name, configured, counts))
            is not None
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

    def validate_password_changed_enough(self, old: str, new: str) -> None:
        """Reject the change when too few characters differ (#3173).

        The edit distance to the current password must reach
        ``KLANGKD_PASSWORD_MIN_CHANGED`` characters. Called only from
        the self-service change-password route, where the old plaintext
        is presented and re-authenticated; reset and admin-set flows
        never see the old plaintext, so the control cannot apply there
        (password-history reuse still does). No-op when the setting is
        0 (the default). Runs before the reuse check — this diff is a
        cheap DP while reuse costs PBKDF2 verifies.
        """
        minimum = self.password_min_changed
        if minimum <= 0:
            return
        distance = password_edit_distance(old, new)
        if distance < minimum:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"New password must change at least {minimum} "
                    "characters from the current password"
                ),
            )

    # --- password age (minimum / maximum; #3177) ---

    def _password_set_at(self, user: dict) -> datetime | None:
        """When *user*'s current password was set, or None when unknown.

        ``users.password_set_at`` is stamped by ``update_password``;
        rows whose password predates the column fall back to
        ``created_at`` — the password is as old as the account, the
        honest upper bound for an age policy enabled after the fact.
        """
        ts = parse_user_ts(user.get("password_set_at"))
        if ts is None:
            ts = parse_user_ts(user.get("created_at"))
        return ts

    def validate_password_min_age(self, user: dict) -> None:
        """Reject a self-service change inside the minimum age.

        No-op when the knob is 0 (disabled) or the set time is unknown.
        Admin-forced resets never call this — the emergency-reset
        exemption.
        """
        hours = self.password_min_age_hours
        if hours <= 0:
            return
        ts = self._password_set_at(user)
        if ts is None:
            return
        remaining = timedelta(hours=hours) - (datetime.now(timezone.utc) - ts)
        if remaining > timedelta(0):
            # Ceiling division: the wait reads "in about N hour(s)",
            # and a 25-minute remainder is still an hour of waiting.
            wait = max(1, -(-int(remaining.total_seconds()) // 3600))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Password must be kept for at least {hours} hour(s);"
                    f" try again in about {wait} hour(s)"
                ),
            )

    def password_expired(self, user: dict) -> bool:
        """True when *user*'s password is past the maximum age.

        Local password accounts only — no ``password_hash`` (OIDC,
        passwordless) means nothing to age. An unknown set time (NULL
        with no ``created_at`` fallback, or unparseable) means "cannot
        judge" → not expired, so a malformed row cannot brick logins.
        """
        if self.password_max_age_days <= 0 or not user.get("password_hash"):
            return False
        ts = self._password_set_at(user)
        return ts is not None and (
            datetime.now(timezone.utc) - ts
            >= timedelta(days=self.password_max_age_days)
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

    async def check_login_lockout(
        self,
        lockout_key: str,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> dict | None:
        """Raise 429 if *lockout_key* is currently locked out.

        Returns the pre-verify ``attempt_info`` (``None`` when lockout
        is disabled) for the caller to hand to
        :meth:`record_login_failure`, which needs it to apply the
        sliding-window reset. An attempt on an already-locked key is
        also audited as a ``login.failed`` row (reason ``locked-out``)
        with the request's workstation metadata (#3205) — the most
        attack-signalling period must not leave the audit stream blank.
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
            await self.app.state.model.audit_events.record_best_effort(
                "login.failed",
                target_type="user",
                detail={
                    "identifier": lockout_key[:AUDIT_IDENTIFIER_MAX],
                    "reason": "locked-out",
                },
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )
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

    def effective_session_idle_minutes(self, is_admin: bool) -> int:
        """The idle-session window for a session owner (#3151).

        Pure helper over the live settings: 0 (off) for everyone; else
        the general window for regular users, and for admins-group
        members the lesser of it and
        :attr:`privileged_session_idle_timeout_minutes` — unless that
        is 0, which turns the privileged split off (admins then use
        the general window).
        """
        window = self.session_idle_timeout_minutes
        if window <= 0:
            return 0
        privileged = self.privileged_session_idle_timeout_minutes
        if is_admin and privileged > 0:
            return min(window, privileged)
        return window

    async def idle_window_minutes_for_user(self, user_id: str) -> int:
        """The caller's idle window, resolving admins-group membership.

        The one DB read per token issue/refresh that keys the 15/10
        split (#3151). Short-circuits to 0 when the timeout is off so
        the unarmed path pays nothing.
        """
        if self.session_idle_timeout_minutes <= 0:
            return 0
        is_admin = await self.app.state.model.users.is_admin(user_id)
        return self.effective_session_idle_minutes(is_admin)

    def _session_stamp_interval(self) -> float:
        """Throttle interval for session last_seen writes (#3151).

        One write per JTI per interval keeps idle-polling clients off
        the write path (the ``users.n_at`` pattern); the interval also
        scales down with the shortest window any user can have — the
        general window, or the privileged one when the split is on and
        shorter — so a 1-minute window is not starved by a 60-second
        stamp lag.
        """
        window = self.shortest_session_idle_minutes
        window_secs = window * 60
        return min(ACTIVITY_STAMP_INTERVAL, window_secs / 4)

    @property
    def shortest_session_idle_minutes(self) -> int:
        """The shortest idle window any user can have (#3151): the
        general window, or the privileged one when the split is on
        and shorter. 0 when the timeout is off."""
        window = self.session_idle_timeout_minutes
        privileged = self.privileged_session_idle_timeout_minutes
        if 0 < privileged < window:
            return privileged
        return window

    def _stamp_throttle_due(self, key: str) -> bool:
        """Record a stamp attempt for *key*; True when a DB write is due.

        The shared per-key clock for both stamp paths (#3151): keys are
        prefixed (``jti:``/``sid:``) so the two namespaces cannot
        collide in one dict.
        """
        now = time.monotonic()
        last = self.session_stamps.get(key)
        if last is not None and now - last < self._session_stamp_interval():
            return False
        while len(self.session_stamps) >= SESSION_STAMP_MAX_ENTRIES:
            del self.session_stamps[next(iter(self.session_stamps))]
        self.session_stamps[key] = now
        return True

    async def record_session_activity(self, jti: str) -> None:
        """Stamp ``user_sessions.last_seen_at`` (throttled) by the
        presented JTI (#3151) — the HTTP request / WS connect path.

        The presented token's JTI always keys the live row (a rotated
        token's old JTI is blocklisted, so requests carrying it never
        get this far). No-op unless the window is armed.
        """
        if self.session_idle_timeout_minutes <= 0:
            return
        if not self._stamp_throttle_due(f"jti:{jti}"):
            return
        await self.app.state.model.sessions.touch_session(jti)

    async def record_ws_session_activity(self, session_id: str) -> None:
        """Stamp ``user_sessions.last_seen_at`` (throttled) by the
        stable session id (#3151) — the WebSocket frame path.

        Frames stamp by ``session_id``, not the connect-time JTI: the
        row is rekeyed on every token refresh, and a socket pinned to
        the old JTI would stamp a row that no longer exists — its
        activity would silently vanish and an actively-used terminal
        session would idle out (~2× the window). No-op unless armed or
        the id no longer resolves (the session was logged out).
        """
        if self.session_idle_timeout_minutes <= 0:
            return
        if not self._stamp_throttle_due(f"sid:{session_id}"):
            return
        await self.app.state.model.sessions.touch_session_by_sid(session_id)

    def create_token(
        self,
        user_id: str,
        email: str,
        expire_hours: float | None = None,
        jkt: str | None = None,
        web_deadline: float | None = None,
    ) -> str:
        """Mint an access token, optionally overriding the lifetime.

        *expire_hours* (when given) is the capped lifetime the idle
        window demands (#3151) — see :meth:`create_capped_token`.
        *jkt* (when given) is an RFC 7638 thumbprint: the token then
        carries ``cnf.jkt`` and every use must present a DPoP proof
        signed by the matching key (#3218). *web_deadline* (when
        given) is the #3230 bind deadline for a web-minted session —
        carried unchanged by every later swap (refresh, bind) so a
        rotation never resets the clock.
        """
        lifetime = (
            self.token_expire_hours if expire_hours is None else expire_hours
        )
        jti = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expire = now + timedelta(hours=lifetime)
        payload = {
            "sub": user_id,
            "email": email,
            "jti": jti,
            "iat": now,
            "exp": expire,
        }
        if jkt is not None:
            payload["cnf"] = {"jkt": jkt}
        if web_deadline is not None:
            payload[BIND_DEADLINE_CLAIM] = web_deadline
        return jwt.encode(payload, self.secret, algorithm=self.algorithm)

    async def create_capped_token(
        self,
        user_id: str,
        email: str,
        jkt: str | None = None,
        web_deadline: float | None = None,
    ) -> str:
        """Mint an access token capped at the owner's idle window (#3151).

        With the window armed, a token that outlives it would let an
        idle session coast past the window on one long-lived token, so
        the lifetime is the lesser of ``KLANGKD_ACCESS_TOKEN_HOURS``
        and the (admin-aware) window. The client's 80%-of-lifetime
        refresh schedule then surfaces it at the refresh seam within
        the window. Unarmed → the plain configured lifetime. *jkt*
        carries a DPoP binding through a refresh (#3218);
        *web_deadline* carries the #3230 bind deadline through it.
        """
        window_hours = (await self.idle_window_minutes_for_user(user_id)) / 60
        lifetime = self.token_expire_hours
        if window_hours > 0:
            lifetime = min(lifetime, window_hours)
        return self.create_token(
            user_id,
            email,
            expire_hours=lifetime,
            jkt=jkt,
            web_deadline=web_deadline,
        )

    async def issue_token(
        self,
        user_id: str,
        email: str,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
        via: str = "password",
        web_client: bool = False,
        jkt: str | None = None,
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
        is still purged of expired rows so it stays bounded). The minted
        token's lifetime is capped at the owner's idle window when the
        idle session timeout is armed (#3151).

        Every mint is also one ``login`` row in the ``audit_events``
        stream (#3205), tagged with *via* — the path that
        authenticated the caller (password, oidc, invite, …) — the
        workstation metadata above, and the request's method/referer
        (#3255) when the minting path had an HTTP request behind it.
        *web_client* (the SPA marks its minting requests, #3230) bakes
        the DPoP bind deadline into the token: an unbound web-minted
        session stops working at the deadline.
        """
        token = await self.create_capped_token(
            user_id,
            email,
            jkt=jkt,
            web_deadline=self._web_deadline(web_client),
        )
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
        await self._enforce_session_limit(
            user_id,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        await self.app.state.model.audit_events.record_best_effort(
            "login",
            actor_id=user_id,
            actor_email=email,
            target_type="user",
            target_id=user_id,
            detail={"via": via},
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
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
        others = _other_workstation_ips(rows, source_ip)
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

    async def _kick_revoked_sockets(self, jti: str) -> None:
        """Close live WS connections authenticated with *jti* (#3152).

        Revocation must cut the sockets the token opened, not just
        reject the next connect — both the main ``/ws`` connections and
        the consent-decider sockets (#3162: a decider holds
        egress-consent authority and lives in its own registry). Minimal
        app states (tests) may not wire ``sockets`` or
        ``consent_deciders`` — then there is nothing to close.
        """
        sockets = getattr(self.app.state, "sockets", None)
        if sockets is not None:
            await sockets.disconnect_by_jti(jti, reason="Token revoked")
        deciders = getattr(self.app.state, "consent_deciders", None)
        if deciders is not None:
            await deciders.disconnect_by_jti(jti, reason="Token revoked")

    async def revoke_all_user_sessions(
        self, user_id: str, *, reason: str = "password change"
    ) -> None:
        """Blocklist and delete every session for *user_id* (#3152).

        Called after a password change (the old credential is invalid, so
        every session minted with it must be forcibly ended) and on user
        deletion (#3195) — both the HTTP side (blocklist → 401) and the
        WebSocket side (kick). *reason* names the trigger in the log.
        """
        rows = await self.app.state.model.sessions.list_sessions(user_id)
        for row in rows:
            logger.info(
                "%s: revoking session jti=%s (user %s)",
                reason,
                row["jti"],
                user_id,
            )
            await self.app.state.model.tokens.blocklist_token(
                row["jti"], row["expires_at"]
            )
            await self._kick_revoked_sockets(row["jti"])
            self.session_stamps.pop(f"jti:{row['jti']}", None)
        if rows:
            await self.app.state.model.sessions.remove_sessions(
                [row["jti"] for row in rows]
            )

    async def _revoke_sessions(
        self,
        user_id: str,
        rows: list[dict],
        limit: int,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> None:
        """Blocklist then delete the given (oldest-first) session rows.

        Victims are removed oldest-first by blocklisting their JTIs (the
        same revocation path as logout: the next HTTP request 401s with
        "Token has been revoked"; the next WebSocket connect is rejected
        with 4001 → client logout), then their session rows are deleted,
        and their live WebSocket connections are closed (#3152).
        Blocklisting happens BEFORE the delete so a crash between the two
        can only leave a dead token's row behind (purged on the next
        issuance), never a live token without a row.
        """
        for row in rows:
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
            await self._kick_revoked_sockets(row["jti"])
            self.session_stamps.pop(f"jti:{row['jti']}", None)
        await self.app.state.model.sessions.remove_sessions(
            [row["jti"] for row in rows]
        )
        await self.app.state.model.audit_events.record_best_effort(
            "session.revoke",
            target_type="session",
            target_id=user_id,
            detail={
                "reason": "session-limit",
                "revoked": len(rows),
                "cap": limit,
            },
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )

    async def _enforce_session_limit(
        self,
        user_id: str,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> None:
        """Revoke the user's oldest sessions past the configured cap.

        Dead sessions (their JWT already failed exp verification) never
        count toward the cap; the purge also bounds the table when the
        limit is off.
        """
        sessions = self.app.state.model.sessions
        await sessions.purge_expired()
        limit = self.max_sessions_per_user
        if limit <= 0:
            return
        rows = await sessions.list_sessions(user_id)
        if len(rows) > limit:
            await self._revoke_sessions(
                user_id,
                rows[:-limit],
                limit,
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )

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
        retain ids of deleted users for the process lifetime. The stamp
        is a write throttle only — pruning it is hygiene, not a
        correctness concern (every ``record_activity`` call site drops
        out on the missing user row anyway).
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

    def create_verification_token(self, user_id: str, email: str) -> str:
        """Create a JWT token for email verification.

        #3201: the token carries the address it was minted for, so a
        link survives neither a later email change (the row's address
        no longer matches) nor its own redemption (a verified row
        rejects it) — one-time, single-purpose.
        """
        return self._create_purpose_token(
            user_id,
            "verify",
            self.verify_token_expire_hours,
            extra={"email": email},
        )

    def decode_verification_token(self, token: str) -> tuple[str, str] | None:
        """Decode a verification token. Returns ``(user_id, email)`` or
        None if invalid."""
        payload = self._decode_purpose_token(token, "verify")
        if payload is None:
            return None
        user_id = payload.get("sub")
        email = payload.get("email")
        if not user_id or not email:
            return None
        return (user_id, email)

    # --- password-reset tokens ---

    @staticmethod
    def reset_token_binding(password_hash: str | None) -> str:
        """The state a reset token is bound to (#3201): a digest of the
        password hash it was minted against. Redemption requires the
        row's current hash to still match, so the first successful
        reset (which rewrites the hash) consumes every outstanding
        token for the account — one-time without server-side state."""
        return hashlib.sha256(
            (password_hash or "").encode("utf-8")
        ).hexdigest()[:16]

    def create_password_reset_token(self, user_id: str, binding: str) -> str:
        """Create a JWT token for password reset."""
        return self._create_purpose_token(
            user_id,
            "reset",
            self.reset_token_expire_hours,
            extra={"pwb": binding},
        )

    def decode_password_reset_token(
        self, token: str
    ) -> tuple[str, str] | None:
        """Decode a password reset token. Returns ``(user_id, binding)``
        or None."""
        payload = self._decode_purpose_token(token, "reset")
        if payload is None:
            return None
        user_id = payload.get("sub")
        binding = payload.get("pwb")
        if not user_id or not binding:
            return None
        return (user_id, binding)

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

    # --- one-time OIDC login codes (#3201) ---

    def mint_login_code(self, access_token: str, email: str) -> str:
        """Mint a single-use, short-lived code standing in for *access_token*.

        The OIDC callback redirects the code to the completer page/CLI
        so the session JWT never rides a URL; the completer redeems it
        via :meth:`redeem_login_code`. Expired entries are pruned on
        every mint (bounded by the mint rate).
        """
        code = secrets.token_urlsafe(32)
        now = time.time()
        self._prune_login_codes(now)
        self.pending_login_codes[code] = (
            access_token,
            email,
            now + self.login_code_ttl_seconds,
        )
        return code

    def _prune_login_codes(self, now: float) -> None:
        """Drop expired login codes (single-use redemption pops the rest)."""
        for stale in [
            c
            for c, (_, _, exp) in self.pending_login_codes.items()
            if exp <= now
        ]:
            del self.pending_login_codes[stale]

    def redeem_login_code(self, code: str) -> tuple[str, str] | None:
        """Pop and return ``(access_token, email)`` behind *code*, or None
        when the code is unknown, already redeemed, or expired."""
        entry = self.pending_login_codes.pop(code, None)
        if entry is None:
            return None
        access_token, email, exp = entry
        if exp <= time.time():
            return None
        return (access_token, email)

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

    async def _authenticated_expired_user(
        self,
        req: ChangeExpiredPasswordRequest,
        source_ip: str | None,
        user_agent: str | None,
        method: str | None = None,
        referer: str | None = None,
    ) -> dict:
        """Resolve and gate a change-expired-password caller.

        The same lockout / credential / verified / disabled gates as
        :meth:`login` (the current password is the ownership proof),
        plus the expiry requirement that makes this endpoint a safe
        minimum-age exemption: it can never act as a general
        change-password bypass because ``password_expired`` is
        re-checked server-side.
        """
        user = await self.app.state.model.users.get_user_by_identifier(
            req.identifier
        )
        lockout_key = user["email"] if user else req.identifier
        attempt_info = await self.check_login_lockout(
            lockout_key,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        await self._reject_bad_credentials(
            user,
            req.current_password,
            lockout_key,
            attempt_info,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        if not user.get("verified"):
            raise HTTPException(
                status_code=403,
                detail="Account not verified. Check your email.",
            )
        ensure_not_disabled(user)
        await self.clear_login_failures(lockout_key)
        if not self.password_expired(user):
            raise HTTPException(
                status_code=400,
                detail="Password has not expired; use change-password",
            )
        return user

    async def change_expired_password(
        self,
        req: ChangeExpiredPasswordRequest,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
        web_client: bool = False,
        jkt: str | None = None,
    ) -> TokenResponse:
        """Replace an expired password and mint the session (#3177).

        The minimum age is still validated below, but that
        is free for any sane config: when min ≤ max, an expired
        password is necessarily past the minimum too, so the check
        never fires — it only stops a min > max misconfig from turning
        the expiry flow into a change-password/history-cycling bypass.
        Auto-logins on success (same posture as reset-password) so
        clients finish in one round trip.
        """
        user = await self._authenticated_expired_user(
            req, source_ip, user_agent, method, referer
        )
        self.validate_password_min_age(user)
        self.validate_password(req.new_password)
        await self.validate_password_not_reused(user["id"], req.new_password)
        password_hash = await asyncio.to_thread(
            hash_password, req.new_password
        )
        await self.app.state.model.users.update_password(
            user["id"], password_hash
        )
        # The expired-password change is a password change (#3205) —
        # its own audit row, before the auto-login's ``login`` row.
        await self.app.state.model.audit_events.record_best_effort(
            "user.password.change",
            actor_id=user["id"],
            actor_email=user["email"],
            target_type="user",
            target_id=user["id"],
            detail={"via": "expired-password"},
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        token = await self.issue_token(
            user["id"],
            user["email"],
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
            via="expired-password",
            web_client=web_client,
            jkt=jkt,
        )
        await self.app.state.model.users.record_login(user["id"])
        return TokenResponse(access_token=token)

    async def register(
        self,
        req: RegisterRequest,
        verified: bool = False,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
        web_client: bool = False,
        jkt: str | None = None,
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
        # The account creation is audited (#3205); the auto-login below
        # adds its own ``login`` row when a session is minted.
        await self.app.state.model.audit_events.record_best_effort(
            "user.register",
            actor_id=user["id"],
            actor_email=user["email"],
            target_type="user",
            target_id=user["id"],
            detail={"email": user["email"], "verified": verified},
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        token = None
        if verified:
            token = await self.issue_token(
                user["id"],
                user["email"],
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
                via="register",
                web_client=web_client,
                jkt=jkt,
            )
            await self.app.state.model.users.record_login(user["id"])
        return RegisterResult(
            user_id=user["id"], email=user["email"], access_token=token
        )

    async def _reject_bad_credentials(
        self,
        user: dict | None,
        password: str,
        lockout_key: str,
        attempt_info,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> None:
        """401 unless a real password hash matched; records the failure
        on the lockout key. A failed credential check is also one
        ``login.failed`` row in the ``audit_events`` stream (#3205) —
        actor-less (the attempter is unauthenticated), targeted at the
        account when it resolves, with the attempted identifier in the
        detail."""
        password_ok = await verify_login_password(user, password)
        if user is None or not user.get("password_hash") or not password_ok:
            await self.app.state.model.audit_events.record_best_effort(
                "login.failed",
                target_type="user",
                target_id=user["id"] if user else None,
                detail={"identifier": lockout_key[:AUDIT_IDENTIFIER_MAX]},
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )
            await self.record_login_failure(lockout_key, attempt_info)
            raise HTTPException(status_code=401, detail="Invalid credentials")

    async def login(
        self,
        req: LoginRequest,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
        web_client: bool = False,
        jkt: str | None = None,
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
        attempt_info = await self.check_login_lockout(
            lockout_key,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        await self._reject_bad_credentials(
            user,
            req.password,
            lockout_key,
            attempt_info,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        if not user.get("verified"):
            raise HTTPException(
                status_code=403,
                detail="Account not verified. Check your email.",
            )
        # A disabled account has valid credentials but must not mint a
        # session (#2588) — admin re-enable is the only way back.
        ensure_not_disabled(user)

        await self.clear_login_failures(lockout_key)
        # Expired passwords stop here: the credentials are
        # valid (failures cleared above), but no session is minted until
        # the password is changed through the expired-password flow.
        if self.password_expired(user):
            raise password_expired_error()
        token = await self.issue_token(
            user["id"],
            user["email"],
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
            via="password",
            web_client=web_client,
            jkt=jkt,
        )
        # Stamp after minting, matching every other session-issuing site
        # (#2583): if minting fails, no login is recorded.
        await self.app.state.model.users.record_login(user["id"])
        return TokenResponse(
            access_token=token,
            must_change_password=user.get("must_change_password", False),
        )

    async def _cached_refresh_response(self, cached: str) -> TokenResponse:
        """A cached refresh replacement stamped with the *live*
        must_change_password flag (#3172) — the flag can flip (admin
        reset) after the replacement was minted, so the cached token's
        own issue-time state is not authoritative."""
        flag = False
        try:
            payload = self.decode_token(cached)
            user = await self.app.state.model.users.get_user_by_id(
                payload.get("sub", "")
            )
            if user is not None:
                flag = user.get("must_change_password", False)
        except JWTError:
            pass
        return TokenResponse(access_token=cached, must_change_password=flag)

    async def _refreshed_or_revoked(
        self, jti: str, workstation=None
    ) -> TokenResponse | None:
        """The cached replacement when *jti* was already refreshed.

        ``None`` when the jti is not blocklisted at all; a blocklisted
        jti with no cached replacement was revoked by logout → 401.
        With binding armed (#3194) the cached replacement is handed
        over only when the caller presents the workstation the
        (rekeyed) session is bound to — the already-refreshed old
        token is exactly what a thief replays here.
        """
        if not await self.app.state.model.tokens.is_token_blocklisted(jti):
            return None
        cached = await self.app.state.model.tokens.get_refreshed_token(jti)
        if cached is not None:
            await self._reject_replayed_cached(cached, workstation)
            return await self._cached_refresh_response(cached)
        raise HTTPException(status_code=401, detail="Token has been revoked")

    async def _require_active_user(self, user_id: str) -> dict:
        """The user row for a token rotation; 401 when the user is gone
        and 403 when disabled (a disabled account cannot rotate its way
        back in, #2588) or the password has expired (#3177 — refresh
        keeps an expired password from stretching a session past the
        maximum age)."""
        user = await self.app.state.model.users.get_user_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        ensure_not_disabled(user)
        if self.password_expired(user):
            raise password_expired_error()
        return user

    async def _swap_token(
        self, jti: str, exp, user_id: str, new_token: str
    ) -> None:
        """Blocklist the old jti with the new token cached alongside it
        (making refresh idempotent), then move the session row onto the
        refreshed jti — a refresh is the same session under a new token
        (#2585), so the user's session count does not grow on refresh."""
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        logger.info(
            "REFRESH: blocklisting old access token; any WS still using "
            "it will be rejected as 4001 on its next connect"
        )
        await self.app.state.model.tokens.blocklist_token(
            jti, expires_at, new_token=new_token
        )
        new_payload = self.decode_token(new_token)
        new_expires_at = datetime.fromtimestamp(
            new_payload["exp"], tz=timezone.utc
        ).isoformat()
        await self.app.state.model.sessions.replace_session(
            jti, user_id, new_payload["jti"], new_expires_at
        )
        self._retarget_refreshed_sockets(
            jti, new_payload["jti"], new_exp=self.ws_expiry(new_payload)
        )
        # The old JTI is dead — drop its stamp-throttle entry (#3151)
        # so the dict tracks live sessions, not history.
        self.session_stamps.pop(f"jti:{jti}", None)

    def _retarget_refreshed_sockets(
        self, old_jti: str, new_jti: str, new_exp: float | None = None
    ) -> None:
        """Move live WS connections onto the refreshed token's JTI (#3152).

        Keeps ``conn.jti`` equal to the session row's current JTI so a
        later hard revocation (logout, eviction) still finds the socket
        the refreshed session is using — for the main ``/ws``
        connections and the consent-decider registrations alike
        (#3162). *new_exp* re-arms each socket's expiry close task for
        the replacement token (#3230) so a rotation never leaves a
        socket closing at the OLD token's expiry (or bind deadline)
        mid-session. Minimal app states (tests) may not wire
        ``sockets`` or ``consent_deciders`` — then there is nothing to
        retarget.
        """
        sockets = getattr(self.app.state, "sockets", None)
        if sockets is not None:
            sockets.reattach_jti(old_jti, new_jti, new_exp=new_exp)
        deciders = getattr(self.app.state, "consent_deciders", None)
        if deciders is not None:
            deciders.reattach_jti(old_jti, new_jti, new_exp=new_exp)

    async def _expired_token_response(
        self, token: str, workstation=None
    ) -> TokenResponse:
        """A previously-refreshed expired token still returns its cached
        replacement; anything else is a plain 401. The cached handover
        carries the same binding gate as the live path (#3194)."""
        payload = self.decode_token(token, allow_expired=True)
        jti = payload.get("jti")
        if jti:
            cached = await self.app.state.model.tokens.get_refreshed_token(jti)
            if cached is not None:
                await self._reject_replayed_cached(cached, workstation)
                return await self._cached_refresh_response(cached)
        raise HTTPException(status_code=401, detail="Token expired")

    async def _session_idle_seconds(self, jti: str) -> float | None:
        """Seconds since the session's last seen stamp; ``None`` when it
        cannot be judged (no session row — a pre-#2585 token — or an
        unparseable stamp). Callers fail open on ``None``, the same
        posture as every other session-row-tolerant path (#3151)."""
        last_seen = await self.app.state.model.sessions.get_last_seen(jti)
        if last_seen is None:
            return None
        try:
            last = datetime.fromisoformat(last_seen)
        except ValueError:
            return None
        if last.tzinfo is None:
            # The m0030 backfill copied SQLite's datetime('now') — naive
            # UTC in the space-separated form. Assume UTC rather than
            # crashing the subtraction (an aware-minus-naive TypeError
            # would 500 the refresh seam it is meant to guard).
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds()

    async def _revoke_session(self, jti: str, exp) -> None:
        """Fully terminate the session behind *jti*: blocklist the token
        (no cached replacement, so any later use 401s as revoked), drop
        its session row, and cut its live sockets — the same complete
        revocation as logout. Shared by logout, the idle-session
        termination (#3151), and the workstation-binding rejection
        (#3194). *exp* is the token's Unix-epoch ``exp`` claim.
        """
        expires_at = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
        await self.app.state.model.tokens.blocklist_token(jti, expires_at)
        await self.app.state.model.sessions.remove_session(jti)
        await self._kick_revoked_sockets(jti)
        self.session_stamps.pop(f"jti:{jti}", None)

    async def _revoke_idle_session(self, jti: str, exp) -> None:
        """Terminate an idle session: blocklist the token (no cached
        replacement, so any later use 401s as revoked), drop its session
        row, and cut its live sockets (#3151) — the same full revocation
        as logout, immediately rather than sweep-lagged: a consent
        decider socket is in no idle sweep, so without the kick it
        would hold egress-consent authority past the termination."""
        logger.info(
            "session idle timeout: revoking jti=%s (idle past the"
            " configured window)",
            jti,
        )
        await self._revoke_session(jti, exp)
        raise HTTPException(
            status_code=401,
            detail="Session timed out due to inactivity",
        )

    async def _reject_idle_session(self, jti: str, exp, user_id: str) -> None:
        """The idle check at the refresh seam (#3151).

        With the window armed, a session whose last real activity
        (HTTP request or WebSocket frame — never a refresh) is older
        than the owner's window is terminated instead of rotated.
        Enforcing here rather than per request keeps every API call
        off the enforcement path while bounding an idle session's life
        to the window plus one refresh interval.
        """
        window = await self.idle_window_minutes_for_user(user_id)
        if window <= 0:
            return
        idle_secs = await self._session_idle_seconds(jti)
        if idle_secs is None or idle_secs <= window * 60:
            return
        await self._revoke_idle_session(jti, exp)

    async def reject_replayed_session(
        self,
        jti: str,
        exp,
        request: Request | None = None,
        workstation: tuple[str | None, str | None] | None = None,
        user_id: str | None = None,
    ) -> bool:
        """True — after revoking the session — when the token is
        presented from a different workstation than it was issued to
        (#3194).

        Replay protection for bearer JWTs: with binding armed
        (``KLANGKD_SESSION_WORKSTATION_BINDING`` = ip|strict), a token captured on
        the wire cannot be used from another machine — the mismatch
        revokes the session (blocklist + row delete + socket kick), an
        audit record names both workstations, and the caller rejects
        the request. The legitimate client shares the token, so it is
        logged out too and must re-authenticate. ``False`` (no action)
        when binding is off, the session row is missing (a pre-#2585
        token), or the presentation matches. *workstation* is the
        caller-resolved pair (WebSocket connects, refresh); *request*
        is resolved lazily — only on the armed path, so the default
        off mode costs nothing.
        """
        mode = self.session_workstation_binding
        if mode == "off":
            return False
        if workstation is None:
            workstation = _request_workstation(request)
        recorded = await self.app.state.model.sessions.get_workstation(jti)
        if recorded is None:
            return False
        if not workstation_mismatch(recorded, workstation, mode == "strict"):
            return False
        await self._revoke_replayed_session(
            jti, exp, recorded, workstation, user_id=user_id
        )
        return True

    async def _ws_binding_rejected(self, payload, workstation) -> bool:
        """True when the presenting connect's workstation fails the
        session-binding check (#3194) — logged like the other token
        rejects; a workstation-less caller (no presentation to
        compare) never does."""
        if workstation is None:
            return False
        if await self.reject_replayed_session(
            payload["jti"],
            payload.get("exp"),
            workstation=workstation,
            user_id=payload.get("sub"),
        ):
            logger.info(
                "token reject: SESSION BOUND TO A DIFFERENT WORKSTATION"
                " -> WS will close 4001 -> client logout"
            )
            return True
        return False

    async def _reject_replayed_refresh(
        self, jti, exp, workstation, user_id=None
    ) -> None:
        """401 — after revoking — when a refresh is presented from a
        different workstation than the session was issued to (#3194)."""
        if await self.reject_replayed_session(
            jti, exp, workstation=workstation, user_id=user_id
        ):
            raise HTTPException(
                status_code=401,
                detail="Session bound to a different workstation",
            )

    async def _reject_replayed_cached(self, cached: str, workstation) -> None:
        """401 — after revoking the live replacement — before handing a
        cached refresh response to a caller on a different workstation
        than the (rekeyed) session is bound to (#3194).

        The replayed *old* token's row no longer exists (the refresh
        rekeyed it), so the binding check runs against the cached
        replacement's JTI — the row that still records the bound
        workstation. A mismatch revokes the live replacement and 401s:
        the idempotent handover must never disclose a live credential
        to a wrong-workstation caller. A caller with no resolved
        workstation is judged by the session row lookup inside
        :meth:`reject_replayed_session` (fail-open on unknown).
        """
        if workstation is None:
            return
        payload = self.decode_token(cached, allow_expired=True)
        await self._reject_replayed_refresh(
            payload.get("jti"),
            payload.get("exp"),
            workstation,
            user_id=payload.get("sub"),
        )

    async def _revoke_replayed_session(
        self, jti, exp, recorded, workstation, user_id=None
    ) -> None:
        """Audit the binding violation (log line + structured
        ``session.revoke`` row, #3205) and revoke the session (#3194).

        A missing ``exp`` claim (an atypical token) still rejects the
        presentation but skips the blocklist write — there is no
        expiry to record. The structured row carries the *presenting*
        workstation in its source_ip/user_agent columns and the bound
        one in the detail; no actor — the trigger is an unknown
        presenter, not the owner.
        """
        logger.info(
            "audit: session binding violation: jti=%s issued to"
            " ip=%s ua=%s, presented from ip=%s ua=%s; session revoked",
            jti,
            recorded[0],
            recorded[1],
            workstation[0],
            workstation[1],
        )
        await self.app.state.model.audit_events.record_best_effort(
            "session.revoke",
            target_type="session",
            target_id=user_id,
            detail={
                "reason": "workstation-binding",
                "bound_ip": recorded[0],
                "bound_ua": recorded[1],
            },
            source_ip=workstation[0],
            user_agent=workstation[1],
        )
        if exp is not None:
            await self._revoke_session(jti, exp)

    def token_binding(self, payload: dict) -> str | None:
        """The DPoP thumbprint a token is bound to, or None (#3218)."""
        jkt = (payload.get("cnf") or {}).get("jkt")
        return jkt if isinstance(jkt, str) else None

    def ws_expiry(self, payload: dict) -> float | None:
        """When a WebSocket connection authenticated by *payload* must
        close: the token's ``exp``, or — for a still-unbound web-minted
        session — the sooner of the two (#3230).

        The deadline is otherwise enforced only at connect; a socket
        opened inside the grace window would otherwise coast to the
        token's natural expiry. Arming the connection's existing
        expiry timer at the deadline closes it in-band.
        """
        exp = payload.get("exp")
        deadline = payload.get(BIND_DEADLINE_CLAIM)
        bound = self.token_binding(payload) is not None
        if (
            not bound
            and isinstance(deadline, (int, float))
            and isinstance(exp, (int, float))
            and deadline < exp
        ):
            return deadline
        return exp

    def _web_deadline(self, web_client: bool) -> float | None:
        """The #3230 bind deadline for a web-minted session; None for
        CLI/TUI mints and when the grace window is disabled (0)."""
        if not web_client or self.web_bind_grace_seconds <= 0:
            return None
        return time.time() + self.web_bind_grace_seconds

    def bind_deadline_expired(self, payload: dict) -> bool:
        """True when an unbound web-minted session is past its DPoP bind
        deadline (#3230).

        Bound tokens never expire this way — presenting a valid proof
        *is* the bound state. Tokens without the claim (CLI/TUI, and
        web mints under a disabled grace window) never expire this way
        either.
        """
        if self.token_binding(payload) is not None:
            return False
        deadline = payload.get(BIND_DEADLINE_CLAIM)
        if not isinstance(deadline, (int, float)):
            return False
        return time.time() > deadline

    def enforce_bind_deadline(self, payload: dict) -> None:
        """Raise 401 when *payload* is a web-minted session that never
        DPoP-bound within its grace window (#3230).

        The choke-point form of :meth:`bind_deadline_expired`: the HTTP
        dependencies and the refresh seam call this; the WebSocket
        gate uses the boolean form so it can close the socket (4001)
        instead. The 401 makes the client drop the token and
        re-login, re-entering the bind flow under attacker-free
        conditions — or surfacing the sabotage.
        """
        if self.bind_deadline_expired(payload):
            logger.info(
                "token reject: WEB SESSION UNBOUND PAST DPoP BIND "
                "DEADLINE -> client logout + re-login"
            )
            raise HTTPException(
                status_code=401,
                detail="Session expired: DPoP binding required",
            )

    def check_dpop(
        self, proof, method: str, path: str, access_token: str, payload: dict
    ) -> str | None:
        """Verify a DPoP proof for *payload*; None = OK / not bound.

        Unbound tokens (no ``cnf.jkt``) verify trivially — CLI/TUI and
        every pre-#3218 client keeps working untouched; the web client
        binds its tokens at login, so a browser session always carries
        the claim.
        """
        jkt = self.token_binding(payload)
        if jkt is None:
            return None
        return dpop_mod.verify_proof(
            proof,
            method=method,
            path=path,
            access_token=access_token,
            expected_jkt=jkt,
            now=time.time(),
            replay=self.dpop_replay,
        )

    def enforce_dpop(
        self, proof, method: str, path: str, access_token: str, payload: dict
    ) -> None:
        """Raise 401 unless a bound token presents a valid DPoP proof."""
        reason = self.check_dpop(proof, method, path, access_token, payload)
        if reason is not None:
            raise HTTPException(
                status_code=401, detail=f"Invalid DPoP proof: {reason}"
            )

    def _bind_claims(self, payload: dict) -> tuple:
        """The (sub, email, jti, exp) of a session payload, or a 401."""
        claims = (
            payload.get("sub"),
            payload.get("email"),
            payload.get("jti"),
            payload.get("exp"),
        )
        if None in claims:
            raise HTTPException(status_code=401, detail="Invalid token")
        return claims

    def _bind_eligibility(self, payload: dict, jwk) -> str:
        """The new binding's thumbprint, after the refuse-list.

        A token already bound cannot be re-bound (an XSS holding the
        readable JWT must not be able to swap the binding to its own
        key) and the JWK must be a public EC P-256 key. Token expiry is
        already rejected by ``decode_token``'s exp verification.
        """
        if self.token_binding(payload) is not None:
            raise HTTPException(status_code=409, detail="Token already bound")
        jkt = dpop_mod.validate_public_jwk(jwk)
        if jkt is None:
            raise HTTPException(status_code=400, detail="Invalid binding key")
        return jkt

    async def bind_token(
        self,
        token: str,
        jwk,
        *,
        workstation: tuple[str | None, str | None] | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> TokenResponse:
        """Swap an unbound session token for one DPoP-bound to *jwk*.

        The web client calls this right after every session-minting
        flow (#3218): the mint endpoints stay credential-only (CLI,
        TUI, and OIDC flows unchanged), then the browser registers its
        non-extractable WebCrypto key and receives a replacement token
        carrying ``cnf.jkt``. The swap reuses the refresh machinery —
        old JTI blocklisted with the replacement cached (a retried
        bind is idempotent), session row moved, live sockets
        retargeted — and keeps the token's *remaining* lifetime rather
        than extending it.
        """
        try:
            payload = self.decode_token(token)
            user_id, email, jti, exp = self._bind_claims(payload)
            cached = await self._refreshed_or_revoked(jti, workstation)
            if cached is not None:
                return cached
            jkt = self._bind_eligibility(payload, jwk)
            user = await self._require_active_user(user_id)
            remaining_hours = (exp - time.time()) / 3600
            new_token = self.create_token(
                user_id,
                email,
                expire_hours=remaining_hours,
                jkt=jkt,
                web_deadline=payload.get(BIND_DEADLINE_CLAIM),
            )
            await self._swap_token(jti, exp, user_id, new_token)
            # Binding a pre-#2585 token INSERTS a session row (the
            # swap re-keys one), so the cap must hold here too — the
            # same reason refresh enforces (#2585 review). The
            # eviction row carries the binding request's metadata.
            await self._enforce_session_limit(
                user_id,
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )
            await self.app.state.model.audit_events.record_best_effort(
                "session.bind",
                actor_id=user_id,
                actor_email=user["email"],
                target_type="session",
                target_id=user_id,
                detail={"via": "dpop"},
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )
            return TokenResponse(
                access_token=new_token,
                must_change_password=user.get("must_change_password", False),
            )
        except ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid token")

    async def _enforce_limit_with_presentation(
        self,
        user_id: str,
        workstation: tuple[str | None, str | None] | None,
        method: str | None = None,
        referer: str | None = None,
    ) -> None:
        """Session-limit enforcement carrying the presenting request's
        metadata (#3255): an eviction row minted by a refresh names
        the workstation pair the refresh presented (unknown when no
        pair was resolved) plus its method/referer."""
        ip, agent = workstation if workstation is not None else (None, None)
        await self._enforce_session_limit(
            user_id,
            source_ip=ip,
            user_agent=agent,
            method=method,
            referer=referer,
        )

    async def refresh_token(
        self,
        token: str,
        workstation: tuple[str | None, str | None] | None = None,
        proof: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> TokenResponse:
        """Exchange a valid access token for a new one.

        The old token's JTI is blocklisted with the new token cached
        alongside it, making the endpoint idempotent: repeated calls
        with the same old token return the same new token. With the
        idle session timeout armed (#3151), an idle session is
        terminated here instead of rotated, and the replacement token
        is capped at the owner's idle window. *workstation* is the
        caller-presented pair; with binding armed (#3194) a token
        presented from a different workstation than it was issued to
        is revoked and refused here too — the refresh seam is the one
        place a headless stolen-token client must eventually surface.
        A DPoP-bound token (#3218) must prove possession to rotate,
        and the replacement keeps the binding.
        """
        try:
            payload = self.decode_token(token)
            user_id = payload.get("sub")
            email = payload.get("email")
            jti = payload.get("jti")
            exp = payload.get("exp")
            if not all([user_id, email, jti, exp]):
                raise HTTPException(status_code=401, detail="Invalid token")

            self.enforce_bind_deadline(payload)

            await self._reject_replayed_refresh(
                jti, exp, workstation, user_id=user_id
            )

            cached = await self._refreshed_or_revoked(jti, workstation)
            if cached is not None:
                return cached

            self.enforce_dpop(
                proof, "POST", "/api/v1/auth/refresh", token, payload
            )

            user = await self._require_active_user(user_id)
            await self._reject_idle_session(jti, exp, user_id)
            # A refresh is authenticated API use (#2588 review): stamp so
            # a headless client that only refreshes (no other API calls)
            # still counts as active. This is the *user-level* clock only
            # — the session-level last_seen is deliberately untouched
            # (#3151), or an idle client that only refreshes would never
            # time out.
            await self.record_activity(user_id)

            new_token = await self.create_capped_token(
                user_id,
                email,
                jkt=self.token_binding(payload),
                web_deadline=payload.get(BIND_DEADLINE_CLAIM),
            )
            await self._swap_token(jti, exp, user_id, new_token)
            # Refreshing a pre-#2585 token (no row) INSERTS one; enforce
            # so the cap holds on every path that adds a session row,
            # not just logins (#2585 review). The eviction row carries
            # the presenting request's metadata (#3255).
            await self._enforce_limit_with_presentation(
                user_id, workstation, method, referer
            )
            return TokenResponse(
                access_token=new_token,
                must_change_password=user.get("must_change_password", False),
            )

        except ExpiredSignatureError:
            # Token expired — check if it was previously refreshed
            return await self._expired_token_response(token, workstation)
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid token")

    async def _token_revoked(self, jti: str) -> bool:
        """True when *jti* was revoked (by a refresh or a logout)."""
        revoked = await self.app.state.model.tokens.is_token_blocklisted(jti)
        if revoked:
            logger.info(
                "token reject: BLOCKLISTED (revoked by a refresh or "
                "logout -> WS will close 4001 -> client logout)"
            )
        return revoked

    async def _user_from_valid_payload(
        self,
        payload: dict,
        workstation: tuple[str | None, str | None] | None = None,
    ) -> dict | None:
        """The user for a decoded, unexpired token payload; ``None`` for
        malformed claims, a revoked token, or a missing user.

        *workstation* (the pair the presenting WebSocket connect
        resolved, #3194) is checked against the session row when
        binding is armed; ``None`` (the request-less legacy callers)
        skips the check — there is no presentation to compare.
        """
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if None in (user_id, jti):
            return None
        if await self._token_revoked(jti):
            return None
        if await self._ws_session_rejected(payload, workstation):
            return None
        user = await self.app.state.model.users.get_user_by_id(user_id)
        reason = self._ws_token_reject_reason(user)
        if reason is not None:
            logger.info(
                "token reject: %s -> WS will close 4001 -> client logout",
                reason,
            )
            return None
        await self.record_activity(user_id)
        await self.record_session_activity(jti)
        return user

    async def _ws_session_rejected(self, payload, workstation) -> bool:
        """True when the session-level gates refuse a WebSocket auth:
        a web-minted session past its DPoP bind deadline (#3230), or a
        workstation-binding mismatch (#3194 — which also revokes and
        logs inside :meth:`_ws_binding_rejected`)."""
        if self.bind_deadline_expired(payload):
            logger.info(
                "token reject: WEB SESSION UNBOUND PAST DPoP BIND "
                "DEADLINE -> WS will close 4001 -> client logout"
            )
            return True
        return await self._ws_binding_rejected(payload, workstation)

    def _ws_token_reject_reason(self, user: dict | None) -> str | None:
        """Why a WS auth must reject *user*; ``None`` when acceptable.
        A missing user row (deleted mid-session account) rejects like
        any dead token."""
        if user is None:
            return "USER NOT FOUND"
        if user.get("disabled"):
            return "ACCOUNT DISABLED"
        if self.password_expired(user):
            return "PASSWORD EXPIRED"
        return None

    async def get_user_from_token(self, token: str) -> dict | str | None:
        """Validate a token string (the request-less legacy/test path).

        Production WebSocket auth routes through ``ws_authenticate`` /
        ``_decider_authenticate``, which pass the connect's resolved
        workstation so the session-binding check runs (#3194); this
        method has no presentation to compare, so binding is NOT
        checked here.

        Returns:
            dict: the user record on success.
            TOKEN_EXPIRED: if the token signature is valid but expired.
            None: for all other failures (malformed, revoked, missing user).
        """
        try:
            return await self._user_from_valid_payload(
                self.decode_token(token)
            )
        except ExpiredSignatureError:
            logger.info(
                "token reject: EXPIRED -> WS will close 4002 -> client logout"
            )
            return self.TOKEN_EXPIRED
        except JWTError:
            return None

    async def logout(self, token: str) -> None:
        """Blocklist the token's JTI, drop its session row, and close the
        WebSocket connections it authenticated (#3152)."""
        try:
            payload = self.decode_token(token)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                await self._revoke_session(jti, exp)
        except JWTError:
            pass


# ---------------------------------------------------------------------------
# FastAPI dependency callables — module-level, reach app.state.auth
# ---------------------------------------------------------------------------


async def _authenticated_user(request: Request, credentials) -> dict:
    """The user for valid, unrevoked credentials; raises 401 for every
    failure mode (missing claims, a revoked token, an unknown user,
    a token presented from a different workstation, #3194)."""
    payload = request.app.state.auth.decode_token(credentials.credentials)
    request.app.state.auth.enforce_dpop(
        request.headers.get("dpop"),
        request.method,
        request.url.path,
        credentials.credentials,
        payload,
    )
    request.app.state.auth.enforce_bind_deadline(payload)
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if None in (user_id, jti):
        raise HTTPException(status_code=401, detail="Invalid token")
    if await request.app.state.model.tokens.is_token_blocklisted(jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    if await request.app.state.auth.reject_replayed_session(
        jti, payload.get("exp"), request=request, user_id=user_id
    ):
        raise HTTPException(
            status_code=401,
            detail="Session bound to a different workstation",
        )
    user = await request.app.state.model.users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    await request.app.state.auth.record_session_activity(jti)
    return user


async def _optional_user(request: Request, credentials) -> dict | None:
    """The user for valid, unrevoked credentials; ``None`` for missing
    claims, a revoked token, an unknown user, or a token presented
    from a different workstation (#3194 — the mismatch still revokes
    the session, then degrades this request to its anonymous view,
    exactly like any other dead token). A DPoP-bound token with a bad
    proof still raises 401 (#3218) — presented credentials that fail
    verification are not "anonymous"."""
    payload = request.app.state.auth.decode_token(credentials.credentials)
    request.app.state.auth.enforce_dpop(
        request.headers.get("dpop"),
        request.method,
        request.url.path,
        credentials.credentials,
        payload,
    )
    request.app.state.auth.enforce_bind_deadline(payload)
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if None in (user_id, jti):
        return None
    if await request.app.state.model.tokens.is_token_blocklisted(jti):
        return None
    if await request.app.state.auth.reject_replayed_session(
        jti, payload.get("exp"), request=request, user_id=user_id
    ):
        return None
    user = await request.app.state.model.users.get_user_by_id(user_id)
    if user is None:
        return None
    await request.app.state.auth.record_session_activity(jti)
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    auth = request.app.state.auth
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        user = await _authenticated_user(request, credentials)
        # A disabled account fails every authenticated request (#2588);
        # 403 (not 401) so clients don't loop on refresh/relogin.
        ensure_not_disabled(user)
        # So does an expired password (#3177) — same posture as disabled,
        # with the machine-readable expiry detail so clients can route
        # to the set-new-password flow instead of looping on refresh.
        if auth.password_expired(user):
            raise password_expired_error()
        # A forced-change account cannot do anything except change its
        # password (#3172); the change-password endpoint uses
        # get_current_user_allow_forced_change instead.
        ensure_password_changed(user)
        await auth.record_activity(user["id"])
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_allow_forced_change(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Like ``get_current_user`` but does not reject sessions under the
    ``must_change_password`` flag (#3172). Used by the change-password
    endpoint so a forced-change user can actually clear the flag.

    Expired passwords still fail (#3177) — they are resolved by the
    unauthenticated ``/auth/change-expired-password`` flow, not here."""
    auth = request.app.state.auth
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        user = await _authenticated_user(request, credentials)
        ensure_not_disabled(user)
        if auth.password_expired(user):
            raise password_expired_error()
        await auth.record_activity(user["id"])
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
        user = await _optional_user(request, credentials)
        if user is None:
            return None
        # Valid credentials on a disabled account still 403 (#2588) —
        # returning None here would silently degrade /config to the
        # anonymous view and hide the reason from the client. An
        # expired password (#3177) signals the same way.
        ensure_not_disabled(user)
        if auth.password_expired(user):
            raise password_expired_error()
        ensure_password_changed(user)
        await auth.record_activity(user["id"])
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
