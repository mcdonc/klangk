"""Authentication routes: register/verify/login/logout, password and email/handle changes, resend-verification, forgot/reset-password, refresh, accept-invite, the proxy auth_request workspace-token validator, and the OIDC login/callback flows (merged from the former oidc_auth submodule)."""

import asyncio
import json
import logging
import secrets
import time
import uuid
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
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

from .. import (
    auth,
    model,
    oidc,
    wshandler,
)
from ..settings import parse_bool_setting
from ..util import API_PREFIX
from .common import get_app_dep, workstation
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
        source_ip, user_agent = workstation(request)
        result = await request.app.state.auth.register(
            req,
            verified=True,
            source_ip=source_ip,
            user_agent=user_agent,
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
        user_id
    )
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    logger.info(
        "Verification URL: %s/#/verify?token=%s...%s",
        f"{proto}://{hostname}{base_path}",
        verification_token[:8],
        verification_token[-4:],
    )

    # Insert user and send email in a transaction — if the email fails,
    # the user insert is rolled back so they can try again.
    async with app.state.model.transaction() as db:
        await app.state.model.users.insert_unverified_user(
            db, user_id, req.email, password_hash
        )
        logger.info("User inserted (uncommitted): %s", req.email)
        await send_email(
            app.state.email.send_verification_email(
                req.email, verification_url
            ),
            req.email,
            "verification email",
        )
        logger.info("Verification email sent, committing user: %s", req.email)

    return {"status": "pending_verification", "email": req.email}


@router.get("/auth/verify")
async def verify_email(token: str, request: Request):
    """Verify a user's email via the token from the verification link."""
    user_id = request.app.state.auth.decode_verification_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification token"
        )
    updated = await request.app.state.model.users.verify_user(user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    user = await request.app.state.model.users.get_user_by_id(user_id)
    # The auto-login must not resurrect a disabled account (#2588).
    auth.ensure_not_disabled(user)
    source_ip, user_agent = workstation(request)
    access_token = await request.app.state.auth.issue_token(
        user_id,
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
    )
    await request.app.state.model.users.record_login(user_id)
    return {"status": "verified", "access_token": access_token}


def prune_timestamps(
    timestamps: dict[str, float], cooldown_seconds: float, now: float
) -> None:
    """Evict rate-limit entries older than their cooldown window.

    The resend/reset rate-limit dicts are keyed by email and gain an
    entry on every request. Without eviction they grow without bound
    and retain raw email addresses (PII) for the process lifetime,
    long past the short cooldown window they're needed for. Opportunistically
    sweeping expired entries on each access bounds both size and retention.
    """
    cutoff = now - cooldown_seconds
    expired = [email for email, ts in timestamps.items() if ts < cutoff]
    for email in expired:
        del timestamps[email]


resend_timestamps: dict[str, float] = {}
RESEND_COOLDOWN_SECONDS = 60


def _rate_limited(timestamps: dict, cooldown: float, email: str) -> bool:
    """True when *email* hit its per-address cooldown window; otherwise
    records this attempt. Bounds both size and retention of the window
    (stale entries are pruned on each call)."""
    now = time.time()
    prune_timestamps(timestamps, cooldown, now)
    last = timestamps.get(email, 0)
    if now - last < cooldown:
        return True
    timestamps[email] = now
    return False


async def _authorize_resend(app, user, req, lockout_key, attempt_info) -> None:
    """401 unless the email+password pair authorizes a resend.

    Same lockout accounting as login (#2618): failures are recorded on
    the lockout key. Without this the endpoint accepted unlimited
    password guesses — the 60s cooldown only bounds email sending and
    only applies after the check succeeds.
    """
    password_ok = await auth.verify_login_password(user, req.password)
    if user is None or not user.get("password_hash") or not password_ok:
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
    attempt_info = await app.state.auth.check_login_lockout(lockout_key)
    await _authorize_resend(app, user, req, lockout_key, attempt_info)
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
        user["id"]
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


@router.post("/auth/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    request: Request,
    app=Depends(get_app_dep),
):
    """Send a password reset email if the account exists."""
    user = await app.state.model.users.get_user_by_email(req.email)
    if user is None:
        # Don't reveal whether the email exists
        return {"status": "sent"}

    # Disabled accounts get no reset email (#2588): the reset itself is
    # refused (403 below), so a link would only confuse — and letting a
    # disabled account drive outbound mail is its own nuisance. Still
    # answer ``"sent"`` so the endpoint never reveals the disabled
    # state to an anonymous caller.
    if user.get("disabled"):
        return {"status": "sent"}

    # Rate limit: one reset email per address per minute
    if _rate_limited(reset_timestamps, RESET_COOLDOWN_SECONDS, req.email):
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    reset_token = request.app.state.auth.create_password_reset_token(
        user["id"]
    )
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    await send_email(
        app.state.email.send_password_reset_email(req.email, reset_url),
        req.email,
        "password reset email",
    )
    return {"status": "sent"}


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest, request: Request):
    """Reset password using a token from the reset email."""
    user_id = request.app.state.auth.decode_password_reset_token(req.token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )
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
    auth.ensure_not_disabled(user)
    await request.app.state.auth.validate_password_not_reused(
        user_id, req.password
    )
    password_hash = await asyncio.to_thread(auth.hash_password, req.password)
    await request.app.state.model.users.update_password(user_id, password_hash)
    # Auto-login after reset
    source_ip, user_agent = workstation(request)
    token = await request.app.state.auth.issue_token(
        user_id,
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
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
    source_ip, user_agent = workstation(request)
    return await request.app.state.auth.login(
        req, source_ip=source_ip, user_agent=user_agent
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
    source_ip, user_agent = workstation(request)
    token = await request.app.state.auth.issue_token(
        user["id"],
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
    )
    await request.app.state.model.users.record_login(user["id"])
    return LocalLoginResponse(access_token=token, email=user["email"])


@router.post("/auth/refresh", response_model=auth.TokenResponse)
async def refresh_token(request: Request):
    """Exchange a valid access token for a new one."""
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    logger.info(
        "REFRESH CALL ua=%s origin=%s referer=%s",
        request.headers.get("user-agent", "?"),
        request.headers.get("origin", "?"),
        request.headers.get("referer", "?"),
    )
    return await request.app.state.auth.refresh_token(token)


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
    user: dict = Depends(auth.get_current_user),
):
    """Change password. Requires current password."""
    await verify_password_confirmation(
        request.app,
        user,
        req.current_password,
        incorrect_detail="Current password is incorrect",
    )
    request.app.state.auth.validate_password(req.new_password)
    await request.app.state.auth.validate_password_not_reused(
        user["id"], req.new_password
    )
    password_hash = await asyncio.to_thread(
        auth.hash_password, req.new_password
    )
    await request.app.state.model.users.update_password(
        user["id"], password_hash
    )
    return {"status": "updated"}


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
    # Mark as unverified and send verification email
    await app.state.model.users.mark_unverified(user["id"])

    hostname, proto, base_path = request.app.state.util.derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    token = request.app.state.auth.create_verification_token(user["id"])
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

    password_hash = await asyncio.to_thread(auth.hash_password, req.password)
    user = await request.app.state.model.users.create_user(
        email, password_hash, verified=True
    )
    await request.app.state.model.invitations.mark_invitation_accepted(
        invitation_id
    )

    source_ip, user_agent = workstation(request)
    access_token = await request.app.state.auth.issue_token(
        user["id"],
        user["email"],
        source_ip=source_ip,
        user_agent=user_agent,
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

    return await users.create_user(
        email=email,
        password_hash=None,
        verified=True,
        provider=provider_id,
        external_id=sub,
    )


def _build_redirect_response(
    request: Request,
    provider_id: str,
    access_token: str,
    cookie_data: dict,
) -> RedirectResponse:
    """Build the final redirect response (CLI or web flow)."""
    cli_redirect = cookie_data.get("cli_redirect")

    if _valid_cli_redirect(cli_redirect):
        redirect_url = f"{cli_redirect}?token={access_token}"
    else:
        hostname, proto, base_path = (
            request.app.state.util.derive_hosting_info(
                request.headers,
                request.client.host if request.client else None,
            )
        )
        redirect_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/oidc-complete?token={access_token}"
        )

    cookie_name = f"oidc_{provider_id}"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
    return response


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

    source_ip, user_agent = workstation(request)
    access_token = await request.app.state.auth.issue_token(
        user["id"],
        email,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    await request.app.state.model.users.record_login(user["id"])
    return _build_redirect_response(
        request, provider_id, access_token, cookie_data
    )


# --- Workspace endpoints ---
