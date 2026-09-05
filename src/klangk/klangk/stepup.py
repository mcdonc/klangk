"""Step-up (sudo-mode) reauthentication for privileged writes (#3196).

The ordinary bearer token authorizes a session until it expires — so a
hijacked or left-unattended admin session could perform every
privileged admin write with no fresh proof of credential knowledge.
This module is the server-side gate that closes that gap:

- ``POST /auth/step-up`` (in :mod:`klangk.api.auth`) verifies the
  caller's password and stamps ``user_sessions.stepped_up_at`` on the
  *calling session's* row.
- :func:`require_step_up` (a FastAPI dependency) and
  :func:`ensure_step_up` (the imperative form, for conditional gates
  decided inside a handler) refuse privileged writes with a
  machine-readable 403 unless that stamp is within
  ``KLANGKD_STEP_UP_WINDOW_MINUTES`` (0, the default, disables the
  gate; 15 minutes is the recommended hardening value).

Design notes:

- **Per session, not per user.** The stamp lives on the session row, so
  a confirmation on one workstation's session never unlocks another
  session of the same user. It dies with the row (logout, eviction,
  revocation) and survives the per-refresh JTI rekeying
  (``replace_session`` carries the column) — a refresh is the same
  session continuing, not a new login.
- **Fail closed.** A token with no session row, a NULL stamp, or an
  unparseable stamp is simply not stepped up. (The idle-timeout reads
  fail open on missing rows because they tolerate pre-#2585 tokens; a
  security gate gets the opposite default.)
- **OIDC-only accounts are exempt, loudly.** They have no klangk
  password to confirm, so requiring the gate would lock them out of
  administration entirely; each exempt pass is audit-logged for
  operators reviewing SIEM output.
- The gate applies to *writes* only — listings and other reads stay on
  the ordinary permission check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from jose import JWTError

from . import auth

logger = logging.getLogger(__name__)

# The machine-readable error code clients detect (CLI retry, web
# password prompt) — the same pattern as auth's "password_expired".
STEP_UP_REQUIRED = "step_up_required"


def step_up_required_error() -> HTTPException:
    """The machine-readable "re-authentication required" 403 (#3196).

    Clients detect the state via ``detail["error"] == "step_up_required"``
    and route to the password-confirmation flow instead of showing a
    bare failure.
    """
    return HTTPException(
        status_code=403,
        detail={
            "error": STEP_UP_REQUIRED,
            "message": (
                "Re-authentication required: confirm your password"
                " via POST /auth/step-up to perform this action"
            ),
        },
    )


def window_minutes(app) -> int:
    """The live step-up window; ``0`` means the gate is disabled."""
    return app.state.settings.step_up_window_minutes


def _parse_stamp(value: str | None) -> datetime | None:
    """A tolerant ISO-8601 parse of ``stepped_up_at``; ``None`` when
    missing or malformed (a corrupt row cannot satisfy the gate — it
    is treated as never stepped up). Naive values (legacy SQLite
    ``datetime('now')`` form) are judged as UTC, the same posture as
    ``Auth._session_idle_seconds``."""
    if value is None:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


async def stepped_up_within(app, jti: str | None, window: int) -> bool:
    """True when *jti*'s session confirmed its password within
    *window* minutes. A missing row, a NULL stamp, or a stamp older
    than the window all read as not stepped up (fail closed)."""
    if jti is None:
        return False
    stamp = await app.state.model.sessions.get_stepped_up_at(jti)
    ts = _parse_stamp(stamp)
    if ts is None:
        return False
    return datetime.now(timezone.utc) - ts <= timedelta(minutes=window)


def _exempt_managed_account(user: dict) -> bool:
    """True for accounts with no klangk password (OIDC-managed).

    They cannot confirm a password they do not have; requiring the
    gate would lock them out of administration entirely. The exemption
    is logged by the caller so the pass is visible to operators.
    """
    return not user.get("password_hash")


async def ensure_step_up(
    request: Request, user: dict, jti: str | None
) -> None:
    """The imperative step-up gate.

    No-op when the window is disabled; exempt (with an audit log) for
    OIDC-managed accounts; otherwise raises the machine-readable 403
    unless the calling session's stamp is fresh. Used directly by
    handlers whose gate condition is only known inside the handler
    (e.g. non-owner workspace deletion) and by :func:`require_step_up`.
    """
    app = request.app
    window = window_minutes(app)
    if window <= 0:
        return
    if _exempt_managed_account(user):
        logger.info(
            "audit: step-up exempt (identity-provider-managed account):"
            " user=%s email=%s",
            user.get("id"),
            user.get("email"),
        )
        return
    if not await stepped_up_within(app, jti, window):
        raise step_up_required_error()


async def confirm_step_up_password(app, user: dict, password: str) -> None:
    """Verify the step-up password with login-grade protections.

    Same lockout accounting as login (failures count toward
    ``KLANGKD_LOGIN_LOCKOUT_*`` on the account's email) and the same
    time-equalized verify, so the step-up endpoint is not a free
    password-guessing oracle for an attacker holding a hijacked
    session. OIDC-managed accounts get the same clear 403 as the
    change-password flow.
    """
    if not user.get("password_hash"):
        raise HTTPException(
            status_code=403,
            detail="Account is managed by your identity provider",
        )
    email = user["email"]
    attempt_info = await app.state.auth.check_login_lockout(email)
    if not await auth.verify_login_password(user, password):
        await app.state.auth.record_login_failure(email, attempt_info)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    await app.state.auth.clear_login_failures(email)


def jti_from_request(app, request: Request) -> str | None:
    """The caller's JTI from the Authorization header.

    For the imperative gate inside handlers that did not receive the
    parsed credentials object. The scheme check is case-insensitive
    (HTTPBearer accepts ``bearer``; re-slicing the header verbatim
    would miss it — see the logout route's note). An unparseable or
    unsigned header yields ``None``, which the gate treats as not
    stepped up; requests reaching a gated handler behind
    ``get_current_user`` always carry a valid Bearer token.
    """
    header = request.headers.get("authorization", "")
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        payload = app.state.auth.decode_token(parts[1])
    except JWTError:
        return None
    return payload.get("jti")


def require_step_up():
    """FastAPI dependency: refuse the request unless the calling
    session confirmed its password within the window.

    Composes with (and runs alongside) the ACL permission dependencies:
    the permission check answers "may this user do this at all", this
    gate answers "has this *session* freshly proven credential
    knowledge". Disabled window → no-op, so routes carry the dependency
    unconditionally and operators arm it with one setting.
    """

    async def check(
        request: Request,
        user: dict = Depends(auth.get_current_user),
        credentials: HTTPAuthorizationCredentials | None = Depends(
            auth.security
        ),
    ) -> None:
        # get_current_user has already validated the token (raising 401
        # otherwise), so the credentials here are non-None and decode
        # deterministically.
        payload = request.app.state.auth.decode_token(credentials.credentials)
        await ensure_step_up(request, user, payload.get("jti"))

    return check
