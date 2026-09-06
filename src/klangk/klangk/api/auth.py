"""Authentication routes: register/verify/login/logout, password and email/handle changes, resend-verification, forgot/reset-password, refresh, accept-invite, the proxy auth_request workspace-token validator, and the OIDC login/callback flows (merged from the former oidc_auth submodule)."""

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
)
from fastapi.security import HTTPAuthorizationCredentials

import httpx
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .. import (
    auth,
    model,
    oidc,
    stepup,
    wshandler,
)
from ..settings import parse_bool_setting
from ..util import API_PREFIX
from ..notifier import notify_event
from .common import get_app_dep, request_metadata
from .common import (
    send_email,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/verify-workspace-token")
async def verify_workspace_token(request: Request):
    """Validate a workspace JWT. Used by the proxy auth_request to gate
    container→host endpoints (/llm-proxy, /api/browser-delegate, etc.)."""
    authorization = request.headers.get("authorization", "")
    fwd_for = request.headers.get("x-forwarded-for", "?")
    fwd_uri = request.headers.get("x-forwarded-uri", "?")
    fwd_method = request.headers.get("x-forwarded-method", "?")
    if not authorization.startswith("Bearer "):
        logger.info(
            "workspace token missing: from=%s method=%s uri=%s",
            fwd_for,
            fwd_method,
            fwd_uri,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing token"},
            headers={"X-Token-Error": "missing"},
        )
    token = authorization[7:]
    result = request.app.state.auth.decode_workspace_token(token)
    if result is auth.Auth.WORKSPACE_TOKEN_EXPIRED:
        logger.info(
            "workspace token expired: from=%s method=%s uri=%s",
            fwd_for,
            fwd_method,
            fwd_uri,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Workspace token expired"},
            headers={"X-Token-Error": "expired"},
        )
    if result is None:
        logger.info(
            "workspace token invalid: from=%s method=%s uri=%s",
            fwd_for,
            fwd_method,
            fwd_uri,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid workspace token"},
            headers={"X-Token-Error": "invalid"},
        )
    return {"status": "ok", "workspace_id": result}


@router.post("/auth/register")
async def register(
    req: auth.RegisterRequest,
    request: Request,
    app=Depends(get_app_dep),
):
    if not request.app.state.oidc.password_login_allowed():
        raise HTTPException(
            status_code=403,
            detail="Password registration is disabled",
        )
    if parse_bool_setting(request.app.state.settings.test_mode):
        # Test mode: auto-verify so E2E tests get immediate access
        source_ip, user_agent, method, referer = request_metadata(request)
        result = await request.app.state.auth.register(
            req,
            verified=True,
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        return result

    logger.info("Registering user: %s", req.email)
    auth.validate_email(req.email)
    existing = await app.state.model.users.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    request.app.state.auth.validate_password(req.password)

    password_hash = await asyncio.to_thread(auth.hash_password, req.password)
    user_id = str(uuid.uuid4())

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    logger.info(
        "Hosting info: hostname=%s proto=%s base_path=%s",
        hostname,
        proto,
        base_path,
    )
    verification_token = request.app.state.auth.create_verification_token(
        user_id, req.email
    )
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    logger.info(
        "Verification email queued for %s (token sha256=%s)",
        req.email,
        hashlib.sha256(verification_token.encode()).hexdigest()[:12],
    )

    # Insert user and send email in a transaction — if the email fails,
    # the user insert is rolled back so they can try again.
    async with app.state.model.transaction() as db:
        await insert_user_or_race_400(app, db, user_id, req, password_hash)
        logger.info("User inserted (uncommitted): %s", req.email)
        await send_email(
            app.state.email.send_verification_email(
                req.email, verification_url
            ),
            req.email,
            "verification email",
        )
        logger.info("Verification email sent, committing user: %s", req.email)

    # The unverified account creation is audited (#3205); there is no
    # actor yet (the registrant is unauthenticated) and no login row
    # until the verification flow mints a session.
    reg_ip, reg_ua, method, referer = request_metadata(request)
    await app.state.model.audit_events.record_best_effort(
        "user.register",
        target_type="user",
        target_id=user_id,
        detail={"email": req.email, "verified": False},
        source_ip=reg_ip,
        user_agent=reg_ua,
        method=method,
        referer=referer,
    )
    notify_event(
        app,
        "user.register",
        target_type="user",
        target_id=user_id,
        detail={"email": req.email, "verified": False},
        source_ip=reg_ip,
    )
    return {"status": "pending_verification", "email": req.email}


async def insert_user_or_race_400(
    app, db, user_id, req, password_hash
) -> None:
    """Insert the unverified user row, mapping a lost race to a 400.

    The duplicate-email pre-check in ``register`` is not atomic with
    the INSERT, so a concurrent registration that wins the UNIQUE
    constraint must surface as the same opaque 400 the pre-check
    returns — no enumeration, no 500 (#3101; production-path twin of
    the #877 fix).
    """
    try:
        await app.state.model.users.insert_unverified_user(
            db, user_id, req.email, password_hash
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=400, detail="Registration failed"
        ) from None


class VerifyRequest(BaseModel):
    token: str


@router.post("/auth/verify")
async def verify_email(req: VerifyRequest, request: Request):
    """Verify a user's email via the token from the verification link.

    #3201: the token rides the request body, not the URL — a GET with a
    ``?token=`` query string would put a bearer-granting credential in
    proxy/server access logs. Redemption is one-time (#3201): the token
    is bound to the address it was minted for, and an already-verified
    row rejects it, so a second click (or a stolen link replayed after
    the first use) cannot mint another session.
    """
    decoded = request.app.state.auth.decode_verification_token(req.token)
    if decoded is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification token"
        )
    user_id, token_email = decoded
    # Atomic one-time redemption (#3201): the conditional UPDATE matches
    # only an unverified row still carrying the minted-for address, so a
    # replay (second click, stolen link) matches nothing and 400s.
    updated = await request.app.state.model.users.verify_user(
        user_id, token_email
    )
    if not updated:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification token"
        )
    user = await request.app.state.model.users.get_user_by_id(user_id)
    if user is None:  # pragma: no cover — the row just transitioned
        raise HTTPException(status_code=404, detail="User not found")
    # The auto-login must not resurrect a disabled account (#2588).
    auth.ensure_not_disabled(user)
    source_ip, user_agent, method, referer = request_metadata(request)
    access_token = await request.app.state.auth.issue_token(
        user_id,
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
        via="email-verify",
    )
    await request.app.state.model.users.record_login(user_id)
    return {"status": "verified", "access_token": access_token}


def prune_timestamps(
    timestamps: dict[str, float], cooldown_seconds: float, now: float
) -> None:
    """Evict rate-limit entries older than their cooldown window.

    The resend/reset rate-limit dicts gain an entry on every recording
    request, each stamped with the then-current monotonic clock, so
    insertion order tracks timestamp order (a monotonic clock never
    steps backwards) and expired entries form a prefix of
    the dict. Evicting from the head costs O(entries dropped) instead
    of the O(size) full scan the unauthenticated forgot-password path
    used to pay on every request (#3113) — a flood of unique fresh
    addresses could not be bounded by a full scan anyway. Keys are
    hashes (see :func:`rate_limit_key`), so nothing retained past the
    window is raw email (PII).
    """
    cutoff = now - cooldown_seconds
    while timestamps:
        oldest = next(iter(timestamps))
        if timestamps[oldest] >= cutoff:
            break
        del timestamps[oldest]


# Upper bound on tracked rate-limit keys per dict. Legitimate traffic
# stays far below it (distinct addresses within one cooldown window);
# a flood of unique submitted strings stops growing the dict at the
# cap, degrading the window to the most recent entries instead of
# exhausting memory and CPU (#3113).
RATE_LIMIT_MAX_ENTRIES = 10_000


# Per-process random key for rate_limit_key: the dicts are process-local
# and never persisted, so the key needs no stability beyond the process.
_RATE_LIMIT_HASH_KEY = secrets.token_bytes(32)


def rate_limit_key(email: str) -> str:
    """Fixed-width rate-limit key for *email*.

    The forgot-password path accepts any submitted string without
    validation (unknown addresses must behave identically to known
    ones, #3100), so raw keys would retain attacker-chosen,
    effectively unbounded-length email strings for the cooldown
    window. The keyed hash bounds per-entry bytes, is not invertible
    by dictionary/rainbow attack over a known-user domain, and cannot
    be correlated across process restarts (#3113).
    """
    return hashlib.blake2b(
        email.encode("utf-8"), key=_RATE_LIMIT_HASH_KEY, digest_size=16
    ).hexdigest()


resend_timestamps: dict[str, float] = {}
RESEND_COOLDOWN_SECONDS = 60


def _rate_limited(timestamps: dict, cooldown: float, email: str) -> bool:
    """True when *email* hit its per-address cooldown window; otherwise
    records this attempt. Bounded regardless of request rate (#3113):
    the check is one dict hit, expired entries are swept only when
    recording, and a full dict sheds its oldest entry before inserting
    — under a unique-address flood the window degrades to the most
    recent ``RATE_LIMIT_MAX_ENTRIES`` addresses instead of growing
    without bound."""
    now = time.monotonic()
    key = rate_limit_key(email)
    last = timestamps.get(key, 0)
    if now - last < cooldown:
        return True
    prune_timestamps(timestamps, cooldown, now)
    while len(timestamps) >= RATE_LIMIT_MAX_ENTRIES:
        del timestamps[next(iter(timestamps))]
    timestamps[key] = now
    return False


async def _authorize_resend(
    app,
    user,
    req,
    lockout_key,
    attempt_info,
    source_ip,
    user_agent,
    method=None,
    referer=None,
) -> None:
    """401 unless the email+password pair authorizes a resend.

    Same lockout accounting as login (#2618): failures are recorded on
    the lockout key (and audited as ``login.failed`` rows, #3205 — a
    failed password check here is a credential guess like any other).
    Without this the endpoint accepted unlimited password guesses —
    the 60s cooldown only bounds email sending and only applies after
    the check succeeds.
    """
    password_ok = await auth.verify_login_password(user, req.password)
    if user is None or not user.get("password_hash") or not password_ok:
        await app.state.model.audit_events.record_best_effort(
            "login.failed",
            target_type="user",
            target_id=user["id"] if user else None,
            detail={
                "identifier": lockout_key[: auth.AUDIT_IDENTIFIER_MAX],
                "path": "resend-verification",
            },
            source_ip=source_ip,
            user_agent=user_agent,
            method=method,
            referer=referer,
        )
        await app.state.auth.record_login_failure(lockout_key, attempt_info)
        raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/auth/resend-verification")
async def resend_verification(
    req: auth.EmailRequest,
    request: Request,
    app=Depends(get_app_dep),
):
    """Resend verification email. Requires email+password to prevent abuse."""
    user = await app.state.model.users.get_user_by_email(req.email)
    # Lockout key: the resolved user's canonical email when known, the
    # raw input for unknown addresses (#2618).
    lockout_key = user["email"] if user else req.email
    source_ip, user_agent, method, referer = request_metadata(request)
    attempt_info = await app.state.auth.check_login_lockout(
        lockout_key,
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    await _authorize_resend(
        app,
        user,
        req,
        lockout_key,
        attempt_info,
        source_ip,
        user_agent,
        method,
        referer,
    )
    if user.get("verified"):
        raise HTTPException(status_code=400, detail="Account already verified")
    await app.state.auth.clear_login_failures(lockout_key)

    # Rate limit: one resend per email per minute
    if _rate_limited(resend_timestamps, RESEND_COOLDOWN_SECONDS, req.email):
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    verification_token = request.app.state.auth.create_verification_token(
        user["id"], user["email"]
    )
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    await send_email(
        app.state.email.send_verification_email(req.email, verification_url),
        req.email,
        "verification email",
    )
    return {"status": "sent"}


class ForgotPasswordRequest(BaseModel):
    email: str


reset_timestamps: dict[str, float] = {}
RESET_COOLDOWN_SECONDS = 60


async def deliver_reset_email(
    email_service, email: str, reset_url: str
) -> None:
    """Background reset-email delivery (#3114).

    Runs after the HTTP response has been sent. Failures are logged
    server-side (operators keep visibility) and never surfaced to the
    anonymous caller — a 503 would only fire on the existing-enabled
    path, making the status code an account-existence oracle.
    """
    try:
        await email_service.send_password_reset_email(email, reset_url)
    except Exception:
        logger.exception("Failed to send password reset email to %s", email)


def _reset_mint_inputs(user: dict | None) -> tuple[bool, str, str | None]:
    """(sendable, subject, password_hash) for a forgot-password mint.

    Unknown addresses still get a subject and a (discarded) binding so
    the mint cost is identical on every path (#3114)."""
    if user is None:
        return False, str(uuid.uuid4()), None
    return not user.get("disabled"), user["id"], user.get("password_hash")


def schedule_reset_delivery(
    background_tasks: BackgroundTasks,
    request: Request,
    email: str,
    user: dict | None,
) -> None:
    """Queue the reset-email delivery off the response path (#3114).

    The endpoint's response — status, body, and timing — must not
    depend on whether *email* names an existing, enabled account.
    Every path mints a reset token, the only response-path work the
    real delivery pays (unknown addresses mint against a nonexistent
    dummy id; the token is discarded), so latency cannot reveal which
    branch ran. Only the existing-enabled path queues the background
    send; disabled accounts still get no email (#2588).
    """
    sendable, subject, password_hash = _reset_mint_inputs(user)
    # #3201: the token is bound to a digest of the current password hash,
    # so the first successful reset (which rewrites the hash) consumes
    # every outstanding token for the account — one-time, stateless.
    reset_token = request.app.state.auth.create_password_reset_token(
        subject,
        request.app.state.auth.reset_token_binding(password_hash),
    )
    if not sendable:
        return
    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers,
        request.client.host if request.client else None,
    )
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    background_tasks.add_task(
        deliver_reset_email, request.app.state.email, email, reset_url
    )


@router.post("/auth/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    app=Depends(get_app_dep),
):
    """Send a password reset email if the account exists.

    The response is byte- and timing-identical for unknown, disabled,
    and existing-enabled addresses (#2588, #3100, #3114): delivery
    happens in a background task after the response is sent, and its
    failures are logged server-side only.
    """
    # Rate limit first, keyed on the submitted address only (#3100): the
    # cooldown must not depend on whether the account exists or is
    # disabled, or its 429 becomes an existence oracle that the
    # ``"sent"``-for-everything posture below exists to prevent.
    if _rate_limited(reset_timestamps, RESET_COOLDOWN_SECONDS, req.email):
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )

    user = await app.state.model.users.get_user_by_email(req.email)
    schedule_reset_delivery(background_tasks, request, req.email, user)
    return {"status": "sent"}


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    """Reset password using a token from the reset email.

    Redemption checks the token's password-hash binding (#3201): a
    token minted before an earlier reset no longer matches the row and
    is rejected, making every reset link one-time.
    """
    decoded = request.app.state.auth.decode_password_reset_token(req.token)
    if decoded is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )
    user_id, binding = decoded
    request.app.state.auth.validate_password(req.password)
    if user_id == model.AGENT_USER_ID:
        raise HTTPException(
            status_code=400,
            detail="Cannot set a password on the system agent user",
        )
    # Refuse the whole reset for a disabled account (#2588) — the
    # password change AND the auto-login below.
    user = await request.app.state.model.users.get_user_by_id(user_id)
    if user is None:  # pragma: no cover — a valid reset token names a
        # live user (the row is only gone if deleted mid-flight)
        raise HTTPException(status_code=404, detail="User not found")
    if (
        request.app.state.auth.reset_token_binding(user.get("password_hash"))
        != binding
    ):
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )
    auth.ensure_not_disabled(user)
    # Self-service resets respect the minimum age (#3177) —
    # only admin-forced resets bypass it.
    request.app.state.auth.validate_password_min_age(user)
    await request.app.state.auth.validate_password_not_reused(
        user_id, req.password
    )
    password_hash = await asyncio.to_thread(auth.hash_password, req.password)
    # A self-chosen password via forgot-password clears the forced-change
    # flag (#3172) — the user chose this password themselves. Hash write
    # and flag clear land in the same transaction.
    await request.app.state.model.users.clear_must_change_password(
        user_id, password_hash
    )
    # The self-chosen reset is a password change (#3205), audited before
    # the auto-login's ``login`` row below.
    source_ip, user_agent, method, referer = request_metadata(request)
    await request.app.state.model.audit_events.record_best_effort(
        "user.password.change",
        actor_id=user_id,
        actor_email=user["email"],
        target_type="user",
        target_id=user_id,
        detail={"via": "password-reset"},
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    notify_event(
        request.app,
        "user.password.change",
        actor_id=user_id,
        actor_email=user["email"],
        target_type="user",
        target_id=user_id,
        detail={"via": "password-reset"},
        source_ip=source_ip,
    )
    # Auto-login after reset
    token = await request.app.state.auth.issue_token(
        user_id,
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
        via="password-reset",
    )
    await request.app.state.model.users.record_login(user_id)
    return {"status": "reset", "access_token": token}


@router.post("/auth/login", response_model=auth.TokenResponse)
async def login(
    req: auth.LoginRequest,
    request: Request,
):
    if not request.app.state.oidc.password_login_allowed():
        raise HTTPException(
            status_code=403, detail="Password login is disabled"
        )
    source_ip, user_agent, method, referer = request_metadata(request)
    return await request.app.state.auth.login(
        req,
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )


class LocalLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


@router.post("/auth/local", response_model=LocalLoginResponse)
async def local_login(request: Request):
    """No-login single-user mode: mint a token for the seeded default
    user, no credentials accepted (#1374).

    Only available when ``KLANGKD_AUTH_MODES=none``. The loopback bind
    (``KLANGKD_LISTEN``) plus the proxy per-location ``allow 127.0.0.1``
    ACL keep this endpoint unreachable from workspace containers; the
    freely-issued Bearer token is kept as belt-and-suspenders CSRF
    defense on every subsequent request.

    As a second belt-and-suspenders layer (and to close the front-proxy
    bypass, where a loopback proxy in front of the proxy makes every request
    appear to come from 127.0.0.1), the backend independently verifies
    the *effective* client is loopback via :func:`util.client_is_loopback`.
    """
    if not request.app.state.oidc.local_login_allowed():
        raise HTTPException(
            status_code=403,
            detail="Local login is not enabled (auth mode is not 'none')",
        )
    # Independent loopback check: trusts X-Real-IP/X-Forwarded-For only when
    # the immediate peer is itself a trusted (loopback) proxy, so it can't be
    # spoofed by a direct non-loopback caller. See util.client_is_loopback.
    if not request.app.state.util.client_is_loopback(
        request.headers, request.client.host if request.client else None
    ):
        raise HTTPException(
            status_code=403,
            detail="Local login requires a loopback client",
        )
    email = request.app.state.settings.default_user
    user = await request.app.state.model.users.get_user_by_email(email)
    if user is None:
        # seed_default_user() runs in the lifespan before the app serves
        # traffic, so this only triggers if seeding was bypassed.
        raise HTTPException(
            status_code=500,
            detail="Default user is not seeded",
        )
    # A disabled default account must not mint a session (#2588).
    auth.ensure_not_disabled(user)
    source_ip, user_agent, method, referer = request_metadata(request)
    token = await request.app.state.auth.issue_token(
        user["id"],
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
        via="local",
    )
    await request.app.state.model.users.record_login(user["id"])
    return LocalLoginResponse(access_token=token, email=user["email"])


@router.post("/auth/refresh", response_model=auth.TokenResponse)
async def refresh_token(request: Request):
    """Exchange a valid access token for a new one.

    A DPoP-bound token (#3218) must present a fresh ``DPoP`` proof
    header; the replacement keeps the binding.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    logger.info(
        "REFRESH CALL ua=%s origin=%s",
        request.headers.get("user-agent", "?"),
        request.headers.get("origin", "?"),
    )
    return await request.app.state.auth.refresh_token(
        token,
        request_metadata(request)[:2],
        proof=request.headers.get("dpop"),
    )


@router.post("/auth/bind", response_model=auth.TokenResponse)
async def bind_session_token(
    request: Request,
    req: auth.BindRequest,
    user: dict = Depends(auth.get_current_user_allow_forced_change),
    credentials: HTTPAuthorizationCredentials | None = Depends(auth.security),
):
    """Swap the caller's session token for one DPoP-bound to a key.

    The web client's post-login step (#3218): it registers the public
    half of its non-extractable WebCrypto ECDSA P-256 keypair and
    receives a replacement token carrying ``cnf.jkt``; every later
    use of that token must present a DPoP proof signed by the private
    half. ``allow_forced_change`` is the dependency so a
    must-change-password session can still bind and keep working
    through the change-password flow (#3172).
    """
    source_ip, user_agent, method, referer = request_metadata(request)
    return await request.app.state.auth.bind_token(
        credentials.credentials,
        req.jwk,
        workstation=(source_ip, user_agent),
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


async def verify_password_confirmation(
    app, user: dict, password: str, *, incorrect_detail: str
) -> None:
    """Re-check the caller's password for a sensitive account change.

    Shared by change-password / change-email / change-handle. OIDC-only
    users have no password; their credentials are managed by their
    identity provider and must not crash on a NULL hash.
    """
    stored = await app.state.model.users.get_user_by_email(user["email"])
    if stored is not None and not stored.get("password_hash"):
        raise HTTPException(
            status_code=403,
            detail="Account is managed by your identity provider",
        )
    if stored is None or not await asyncio.to_thread(
        auth.verify_password, password, stored["password_hash"]
    ):
        raise HTTPException(status_code=401, detail=incorrect_detail)


@router.post("/auth/change-password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user_allow_forced_change),
):
    """Change password. Requires current password.

    Uses ``get_current_user_allow_forced_change`` instead of the standard
    ``get_current_user`` so a session under the ``must_change_password``
    flag can reach this endpoint (#3172).
    """
    await verify_password_confirmation(
        request.app,
        user,
        req.current_password,
        incorrect_detail="Current password is incorrect",
    )
    # Self-service changes respect the minimum age (#3177) — except the
    # forced first change after an admin-set password (#3172): the
    # temporary password must be replaceable *immediately*, and the
    # admin reset that set this password already bypassed the age
    # check.
    if not user.get("must_change_password"):
        request.app.state.auth.validate_password_min_age(user)
    request.app.state.auth.validate_password(req.new_password)
    request.app.state.auth.validate_password_changed_enough(
        req.current_password, req.new_password
    )
    await request.app.state.auth.validate_password_not_reused(
        user["id"], req.new_password
    )
    password_hash = await asyncio.to_thread(
        auth.hash_password, req.new_password
    )
    # clear_must_change_password atomically updates the hash AND clears
    # the flag in one transaction (#3172).
    await request.app.state.model.users.clear_must_change_password(
        user["id"], password_hash
    )
    # Revoke every session so the old credential cannot be reused and
    # any live WebSocket connections are closed (#3152).
    await request.app.state.auth.revoke_all_user_sessions(user["id"])
    # Both halves are audited (#3205): the password change itself, and
    # the session revocation it forced (every live session ended).
    source_ip, user_agent, method, referer = request_metadata(request)
    await request.app.state.model.audit_events.record_best_effort(
        "user.password.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"via": "self-service"},
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    notify_event(
        request.app,
        "user.password.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"via": "self-service"},
        source_ip=source_ip,
    )
    await request.app.state.model.audit_events.record_best_effort(
        "session.revoke",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="session",
        target_id=user["id"],
        detail={"reason": "password-change"},
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return {"status": "updated"}


class StepUpRequest(BaseModel):
    """Sudo-mode password confirmation body (#3196)."""

    password: str


@router.post("/auth/step-up")
async def step_up(
    req: StepUpRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    """Confirm the caller's password for privileged writes (#3196).

    Verifies the password (with login-grade lockout accounting) and
    stamps the confirmation on the calling session's row, clearing it
    for step-up-gated admin writes for
    ``KLANGKD_STEP_UP_WINDOW_MINUTES``. 400 when the window is disabled
    (nothing to confirm); 401 on a bad password or a token with no
    live session row.
    """
    app = request.app
    if stepup.window_minutes(app) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Step-up authentication is not enabled",
        )
    await stepup.confirm_step_up_password(app, user, req.password, request)
    # get_current_user has validated the token, so the header is a
    # valid Bearer; the guarded JTI recovery keeps an exp-boundary race
    # failing closed (401) instead of 500.
    jti = stepup.jti_from_request(app, request)
    stamped = await app.state.model.sessions.stamp_step_up(jti or "")
    if not stamped:
        raise HTTPException(status_code=401, detail="Session not found")
    source_ip, user_agent, method, referer = request_metadata(request)
    await app.state.model.audit_events.record_best_effort(
        "step_up.confirmed",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"window_minutes": stepup.window_minutes(app)},
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    logger.info(
        "step-up: password confirmed for user=%s email=%s",
        user["id"],
        user["email"],
    )
    return {"status": "stepped_up"}


@router.post("/auth/change-expired-password")
async def change_expired_password(
    req: auth.ChangeExpiredPasswordRequest,
    request: Request,
):
    """Replace an expired password and auto-login (#3177).

    Takes the identifier, the current (expired) password, and the new
    one; mints a session on success so clients complete the flow in
    one round trip. The *login* endpoint signals expiry (403 with
    ``detail.error = "password_expired"``); this endpoint resolves it.
    """
    if not request.app.state.oidc.password_login_allowed():
        raise HTTPException(
            status_code=403, detail="Password login is disabled"
        )
    source_ip, user_agent, method, referer = request_metadata(request)
    return await request.app.state.auth.change_expired_password(
        req,
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )


class ChangeEmailRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/change-email")
async def change_email(
    req: ChangeEmailRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    """Change email. Requires password. Marks account as unverified."""
    await verify_password_confirmation(
        app, user, req.password, incorrect_detail="Password is incorrect"
    )
    auth.validate_email(req.email)
    existing = await app.state.model.users.get_user_by_email(req.email)
    if existing is not None and existing["id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Email already in use")
    await app.state.model.users.update_email(user["id"], req.email)
    # The email change is audited the moment it lands (#3205); the
    # verification email below can still fail with the change applied.
    source_ip, user_agent, method, referer = request_metadata(request)
    await app.state.model.audit_events.record_best_effort(
        "user.email.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"email": req.email},
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    notify_event(
        app,
        "user.email.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"email": req.email},
        source_ip=source_ip,
    )
    # Mark as unverified and send verification email
    await app.state.model.users.mark_unverified(user["id"])

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    token = request.app.state.auth.create_verification_token(
        user["id"], req.email
    )
    url = f"{proto}://{hostname}{base_path}/#/verify?token={token}"
    await send_email(
        app.state.email.send_verification_email(req.email, url),
        req.email,
        "verification email",
    )
    return {"status": "updated", "needs_verification": True}


class ChangeHandleRequest(BaseModel):
    handle: str
    password: str


@router.post("/auth/change-handle")
async def change_handle(
    req: ChangeHandleRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    """Change the current user's handle. Requires password confirmation."""
    await verify_password_confirmation(
        app, user, req.password, incorrect_detail="Password is incorrect"
    )
    try:
        await app.state.model.users.set_user_handle(user["id"], req.handle)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await wshandler.refresh_user_handle(
        app.state.sockets, user["id"], req.handle
    )
    source_ip, user_agent, method, referer = request_metadata(request)
    await app.state.model.audit_events.record_best_effort(
        "user.handle.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"handle": req.handle},
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
    )
    notify_event(
        app,
        "user.handle.change",
        actor_id=user["id"],
        actor_email=user["email"],
        target_type="user",
        target_id=user["id"],
        detail={"handle": req.handle},
        source_ip=source_ip,
    )
    return {"status": "updated", "handle": req.handle}


@router.get("/auth/me")
async def get_me(
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    """Return the current user's profile."""
    full = await app.state.model.users.get_user_by_id(user["id"])
    if full is None:  # pragma: no cover — race between auth and lookup
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "id": full["id"],
        "email": full["email"],
        "handle": full["handle"],
        "last_login_at": full.get("last_login_at"),
    }


async def _oidc_logout_url(request: Request, user: dict) -> str | None:
    """The IdP logout URL when *user* is an OIDC user whose provider has
    logout_redirect enabled; ``None`` for local users (or a missing
    provider/URL)."""
    db_user = await request.app.state.model.users.get_user_by_email(
        user["email"]
    )
    if not db_user or db_user.get("provider", "local") == "local":
        return None
    provider = request.app.state.oidc.get_provider(db_user["provider"])
    if not provider:
        return None
    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers,
        request.client.host if request.client else None,
    )
    post_logout_uri = f"{proto}://{hostname}{base_path}/#/login"
    return await request.app.state.oidc.build_logout_url(
        provider, post_logout_uri
    )


@router.post("/auth/logout")
async def logout(
    request: Request,
    user: dict | None = Depends(auth.get_current_user_lenient),
    credentials: HTTPAuthorizationCredentials | None = Depends(auth.security),
):
    # Logout only invalidates credentials -- it deliberately does NOT stop the
    # user's containers. Per #301/#1235 the idle timeout is the only thing
    # that stops containers (plus the explicit
    # ``POST /api/v1/workspaces/{id}/stop`` endpoint, which requires the
    # ``stop-workspace`` permission). Stopping on logout was a holdover from
    # the per-user-container era and destroyed service sessions that should
    # outlive any single user's login.
    # Blocklist the token so it can't be reused after logout. Logout is
    # idempotent (#2687): a token that is already expired, revoked, or
    # logged out has reached the desired end state, so the request still
    # succeeds — hence get_current_user_lenient (never raises, tolerates
    # disabled accounts) and the parsed credentials object (HTTPBearer
    # accepts a case-insensitive scheme, so re-slicing the raw header
    # would miss a lowercase ``bearer`` token).
    if credentials is not None:
        await request.app.state.auth.logout(credentials.credentials)
        if user is not None:
            # The logout is audited (#3205) only when it ended a live
            # session — an anonymous or dead-token call revoked nothing.
            source_ip, user_agent, method, referer = request_metadata(request)
            await request.app.state.model.audit_events.record_best_effort(
                "logout",
                actor_id=user["id"],
                actor_email=user["email"],
                target_type="session",
                target_id=user["id"],
                source_ip=source_ip,
                user_agent=user_agent,
                method=method,
                referer=referer,
            )

    result: dict = {"status": "ok"}
    if user is None:
        # Anonymous/invalid token: nothing left to do. The OIDC logout
        # redirect requires a live user record, so it is skipped.
        return result
    # If the user logged in via OIDC and the provider has logout_redirect
    # enabled, return the IdP logout URL so the frontend can redirect.
    logout_url = await _oidc_logout_url(request, user)
    if logout_url:
        result["oidc_logout_url"] = logout_url
    return result


# --- Invitation endpoints ---


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


async def create_invited_user(
    request: Request, email: str, password: str
) -> dict:
    """Create the verified account for an accepted invitation.

    The duplicate-email pre-check in the caller is not atomic with the
    INSERT, so a concurrent accept (or registration) that wins the
    UNIQUE constraint must surface as the same 400 the pre-check
    returns, not an unhandled 500 (#3101).
    """
    password_hash = await asyncio.to_thread(auth.hash_password, password)
    try:
        return await request.app.state.model.users.create_user(
            email, password_hash, verified=True
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=400,
            detail="An account with this email already exists",
        ) from None


@router.post("/auth/accept-invite")
async def accept_invite(req: AcceptInviteRequest, request: Request):
    """Accept an invitation and create a verified account."""
    result = request.app.state.auth.decode_invitation_token(req.token)
    if result is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired invitation token"
        )
    invitation_id, email = result

    invitation = await request.app.state.model.invitations.get_invitation(
        invitation_id
    )
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=400, detail="Invitation is no longer valid"
        )

    request.app.state.auth.validate_password(req.password)

    existing = await request.app.state.model.users.get_user_by_email(email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="An account with this email already exists"
        )

    user = await create_invited_user(request, email, req.password)
    await request.app.state.model.invitations.mark_invitation_accepted(
        invitation_id
    )
    # The invited account's creation notifies the SA/ISSO stream under
    # the same user.create name as the other creation paths (#3250);
    # the registrant is unauthenticated, so there is no actor.
    source_ip, user_agent, method, referer = request_metadata(request)
    notify_event(
        request.app,
        "user.create",
        target_type="user",
        target_id=user["id"],
        detail={"email": email, "via": "invite"},
        source_ip=source_ip,
    )
    access_token = await request.app.state.auth.issue_token(
        user["id"],
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
        via="invite",
    )
    await request.app.state.model.users.record_login(user["id"])
    return {"status": "accepted", "access_token": access_token}


# ---------------------------------------------------------------------------
# OIDC login/callback (merged from the former oidc_auth submodule; its own
# sub-router definitions are appended verbatim onto this module's router).
# ---------------------------------------------------------------------------


def _is_local_url(parsed) -> bool:
    """The parsed URL must be plain http to loopback with an explicit
    port and no userinfo."""
    return (
        parsed.scheme == "http"
        and parsed.username is None
        and parsed.password is None
        and parsed.hostname in ("localhost", "127.0.0.1")
        and parsed.port is not None
    )


def _valid_cli_redirect(url: str | None) -> bool:
    """True if *url* is a permitted CLI redirect target (localhost only).

    The OIDC state cookie is unsigned and client-controlled, so the
    cli_redirect stored there must be re-validated at callback time —
    otherwise a tampered cookie could redirect the freshly-minted
    access token to an attacker-controlled host (#936).

    The URL is parsed rather than prefix-matched: ``startswith`` is
    blind to userinfo, so ``http://localhost:1@attacker.example/``
    would pass the guard while routing to ``attacker.example`` (#2571).
    """
    if not url:
        return False
    try:
        return _is_local_url(urlparse(url))
    except ValueError:
        # Malformed URL (e.g. non-integer port) — reject, don't 500.
        return False


# --- OIDC endpoints ---


@router.get("/auth/oidc/{provider_id}/login")
async def oidc_login(
    provider_id: str,
    request: Request,
    cli_redirect: str | None = None,
):
    """Redirect to the OIDC IdP for authentication."""
    oidc_inst = request.app.state.oidc
    if not oidc_inst.oidc_login_allowed():
        raise HTTPException(status_code=404, detail="OIDC not enabled")

    provider = oidc_inst.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    # Validate cli_redirect is localhost only (re-checked at callback,
    # since the state cookie storing it is unsigned — see #936).
    if cli_redirect and not _valid_cli_redirect(cli_redirect):
        raise HTTPException(
            status_code=400, detail="cli_redirect must be localhost"
        )

    verifier, challenge = oidc.generate_pkce()
    state = secrets.token_urlsafe(32)

    redirect_uri = _derive_redirect_uri(request, provider_id)

    auth_url = await oidc_inst.build_auth_url(
        provider, redirect_uri, state, challenge
    )

    response = RedirectResponse(url=auth_url, status_code=302)
    # Store state + verifier + cli_redirect in a cookie.  The
    # redirect_uri is deliberately NOT stored: the cookie is unsigned
    # and client-controlled, so the callback re-derives it from hosting
    # info instead of trusting a round-tripped copy (#2573).
    cookie_value = json.dumps(
        {
            "state": state,
            "verifier": verifier,
            "cli_redirect": cli_redirect,
        }
    )
    response.set_cookie(
        key=f"oidc_{provider_id}",
        value=cookie_value,
        httponly=True,
        secure=True,
        max_age=600,
        samesite="lax",
        path="/",
    )
    return response


def _derive_redirect_uri(request: Request, provider_id: str) -> str:
    """Derive the OAuth2 ``redirect_uri`` from hosting info.

    Derived identically at login and callback time (hosting env vars,
    then trusted forwarded headers).  The unsigned state cookie must
    never carry it: a client-controlled ``redirect_uri`` would be fed
    verbatim to the IdP token endpoint (#2573).

    Because the value is derived per request, a hosting-config reload
    (SIGHUP) between login and callback — or a fleet with differing
    proxy-trust settings — can make the two derivations disagree; the
    IdP then rejects the exchange and the login fails loudly with a
    502.  In-flight logins only; retrying after the reload succeeds.
    """
    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    return f"{proto}://{hostname}{base_path}{API_PREFIX}/auth/oidc/{provider_id}/callback"


def _cookie_state_matches(cookie_data, state: str) -> bool:
    """The cookie must decode to a dict whose state echoes the
    callback's."""
    return isinstance(cookie_data, dict) and cookie_data.get("state") == state


def _valid_verifier(verifier) -> bool:
    """The PKCE verifier is required downstream; a cookie missing it
    must 400, not KeyError-500."""
    return isinstance(verifier, str) and bool(verifier)


def _validate_state_cookie(
    request: Request, provider_id: str, state: str
) -> dict:
    """Parse and validate the OIDC state cookie, returning its data."""
    cookie_name = f"oidc_{provider_id}"
    cookie_raw = request.cookies.get(cookie_name)
    if not cookie_raw:
        raise HTTPException(
            status_code=400, detail="Missing OIDC state cookie"
        )

    try:
        cookie_data = json.loads(cookie_raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400, detail="Invalid OIDC state cookie"
        )

    if not _cookie_state_matches(cookie_data, state):
        raise HTTPException(status_code=400, detail="State mismatch")

    if not _valid_verifier(cookie_data.get("verifier")):
        raise HTTPException(
            status_code=400, detail="Invalid OIDC state cookie"
        )

    return cookie_data


def _require_oidc_claims(claims: dict) -> None:
    """502 when the ID token lacks a usable sub/email pair."""
    if not claims.get("sub") or not claims.get("email"):
        raise HTTPException(
            status_code=502,
            detail="ID token missing sub or email claim",
        )


async def _exchange_and_validate_token(
    oidc_inst, provider, code, redirect_uri, verifier
):
    """Exchange the authorization code for tokens and validate the ID token.

    Returns ``(claims, tokens)`` where *claims* contains at least ``sub``
    and ``email``.  *redirect_uri* is the server-derived callback URL —
    never a value read back from the unsigned state cookie (#2573).
    """
    try:
        tokens = await oidc_inst.exchange_code(
            provider,
            code,
            redirect_uri,
            verifier,
        )
    except httpx.HTTPStatusError as exc:
        logger.error("OIDC token exchange failed: %s", exc.response.text)
        raise HTTPException(
            status_code=502, detail="Token exchange failed"
        ) from None

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="No ID token in response")

    try:
        claims = await oidc_inst.validate_id_token(
            provider, id_token, access_token=tokens.get("access_token")
        )
    except Exception as exc:
        logger.error("OIDC ID token validation failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="ID token validation failed"
        ) from None

    _require_oidc_claims(claims)

    return claims, tokens


async def _find_or_create_user(app, provider_id, sub, email):
    """Locate an existing user by OIDC identity or create one via JIT provisioning.

    Raises ``HTTPException(403)`` if the resolved user is the system agent —
    OIDC must never mint a session as the agent (#1225).
    """
    users = app.state.model.users
    user = await users.get_user_by_external_id(provider_id, sub)
    if user is not None:
        if user["id"] == model.AGENT_USER_ID:
            raise HTTPException(
                status_code=403,
                detail="Cannot log in as the system agent",
            )
        return user

    existing = await users.get_user_by_email(email)
    if existing is not None:
        if existing["id"] == model.AGENT_USER_ID:
            raise HTTPException(
                status_code=403,
                detail="Cannot log in as the system agent",
            )
        await users.link_oidc_identity(existing["id"], provider_id, sub)
        return existing

    user = await users.create_user(
        email=email,
        password_hash=None,
        verified=True,
        provider=provider_id,
        external_id=sub,
    )
    # JIT-provisioned account creation notifies the SA/ISSO stream
    # under the same user.create name as the other creation paths
    # (#3250). No actor: the account did not exist before this login,
    # and the IdP — not a klangk principal — vouched for the identity.
    notify_event(
        app,
        "user.create",
        target_type="user",
        target_id=user["id"],
        detail={"email": email, "via": "oidc"},
    )
    return user


def _build_redirect_response(
    request: Request,
    provider_id: str,
    access_token: str,
    email: str,
    cookie_data: dict,
) -> RedirectResponse:
    """Build the final redirect response (CLI or web flow).

    #3201: neither flow places the session JWT in a URL. The callback
    redirects a one-time, 60s code; the CLI/web completer redeems it
    via ``POST /auth/oidc/exchange`` for the token.
    """
    cli_redirect = cookie_data.get("cli_redirect")
    code = request.app.state.auth.mint_login_code(access_token, email)

    if _valid_cli_redirect(cli_redirect):
        redirect_url = f"{cli_redirect}?code={code}"
    else:
        hostname, proto, base_path = (
            request.app.state.util.derive_hosting_info(
                request.headers,
                request.client.host if request.client else None,
            )
        )
        redirect_url = (
            f"{proto}://{hostname}{base_path}/#/oidc-complete?code={code}"
        )

    cookie_name = f"oidc_{provider_id}"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
    return response


class OidcExchangeRequest(BaseModel):
    code: str


@router.post("/auth/oidc/exchange")
async def oidc_exchange(req: OidcExchangeRequest, request: Request):
    """Redeem a one-time OIDC login code for the session token (#3201).

    The callback redirect carries only a single-use, 60-second code
    (never the JWT itself); the completer — the frontend's
    ``/#/oidc-complete`` page or the CLI's localhost callback — swaps
    it for the access token here. Unknown, replayed, or expired codes
    all fail identically with 400.
    """
    redeemed = request.app.state.auth.redeem_login_code(req.code)
    if redeemed is None:
        raise HTTPException(status_code=400, detail="Invalid login code")
    token, email = redeemed
    return {"access_token": token, "email": email}


def _require_verified_email(provider, claims: dict) -> None:
    """403 unless the IdP verified the email claim — or the operator
    trusts this IdP's email claims (``trust-email: true`` in the
    provider config)."""
    if not provider.trust_email and claims.get("email_verified") is not True:
        raise HTTPException(
            status_code=403,
            detail="Email not verified by identity provider",
        )


async def _call_login_hook(request: Request, provider, claims, email, tokens):
    """Run the OIDC login hook (if configured); any failure denies the
    login (403)."""
    try:
        return await request.app.state.oidc.call_login_hook(
            provider, claims, email, tokens
        )
    except Exception:
        logger.exception("OIDC login hook failed for provider %s", provider)
        raise HTTPException(
            status_code=403,
            detail="Login denied by server policy",
        ) from None


@router.get("/auth/oidc/{provider_id}/callback")
async def oidc_callback(
    provider_id: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str | None = None,
):
    """Handle the OIDC callback from the IdP."""
    if error:
        logger.warning(
            "OIDC IdP error for provider %s: %s", provider_id, error
        )
        raise HTTPException(status_code=400, detail="Login failed")

    provider = request.app.state.oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    cookie_data = _validate_state_cookie(request, provider_id, state)
    claims, tokens = await _exchange_and_validate_token(
        request.app.state.oidc,
        provider,
        code,
        _derive_redirect_uri(request, provider_id),
        cookie_data["verifier"],
    )

    email = claims["email"]
    auth.validate_email(email)

    # Require email_verified unless the operator trusts this IdP's
    # email claims (trust-email: true in the provider config).
    _require_verified_email(provider, claims)

    # Call the OIDC login hook (if configured).
    hook_groups = await _call_login_hook(
        request, provider, claims, email, tokens
    )

    user = await _find_or_create_user(
        request.app, provider_id, claims["sub"], email
    )
    # A disabled account must not mint a session via SSO (#2588).
    auth.ensure_not_disabled(user)

    if hook_groups is not None:
        await request.app.state.oidc.sync_oidc_groups(user["id"], hook_groups)

    source_ip, user_agent, method, referer = request_metadata(request)
    access_token = await request.app.state.auth.issue_token(
        user["id"],
        email,
        source_ip=source_ip,
        user_agent=user_agent,
        method=method,
        referer=referer,
        via="oidc",
    )
    await request.app.state.model.users.record_login(user["id"])
    return _build_redirect_response(
        request, provider_id, access_token, email, cookie_data
    )


# --- Workspace endpoints ---
