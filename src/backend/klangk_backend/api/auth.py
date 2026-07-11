"""Authentication routes: register/verify/login/logout, password and email/handle changes, resend-verification, forgot/reset-password, refresh, accept-invite, and the nginx auth_request workspace-token validator."""

import logging
import time
import uuid

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import (
    JSONResponse,
)
from pydantic import BaseModel

from .. import (
    auth,
    emailsvc,
    model,
    oidc,
    wshandler,
)
from ..settings import get_settings
from ..util import (
    client_is_loopback,
    derive_hosting_info,
    resolve_env_value,
)
from ._common import (
    send_email,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/verify-workspace-token")
async def verify_workspace_token(request: Request):
    """Validate a workspace JWT. Used by nginx auth_request to gate
    container→host endpoints (/llm-proxy, /api/browser-delegate, etc.)."""
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing token"},
            headers={"X-Token-Error": "missing"},
        )
    token = authorization[7:]
    result = auth.decode_workspace_token(token)
    if result is auth.WORKSPACE_TOKEN_EXPIRED:
        return JSONResponse(
            status_code=401,
            content={"detail": "Workspace token expired"},
            headers={"X-Token-Error": "expired"},
        )
    if result is None:
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
):
    if not oidc.password_login_allowed(get_settings()):
        raise HTTPException(
            status_code=403,
            detail="Password registration is disabled",
        )
    if resolve_env_value("KLANGK_TEST_MODE"):
        # Test mode: auto-verify so E2E tests get immediate access
        result = await auth.register(req, verified=True)
        return result

    logger.info("Registering user: %s", req.email)
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Registration failed")
    auth.validate_password_length(req.password)

    password_hash = auth.hash_password(req.password)
    user_id = str(uuid.uuid4())

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    logger.info(
        "Hosting info: hostname=%s proto=%s base_path=%s",
        hostname,
        proto,
        base_path,
    )
    verification_token = auth.create_verification_token(user_id)
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
    async with model.transaction() as db:
        await model.insert_unverified_user(
            db, user_id, req.email, password_hash
        )
        logger.info("User inserted (uncommitted): %s", req.email)
        await send_email(
            emailsvc.send_verification_email(req.email, verification_url),
            req.email,
            "verification email",
        )
        logger.info("Verification email sent, committing user: %s", req.email)

    return {"status": "pending_verification", "email": req.email}


@router.get("/auth/verify")
async def verify_email(token: str):
    """Verify a user's email via the token from the verification link."""
    user_id = auth.decode_verification_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification token"
        )
    updated = await model.verify_user(user_id)
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    user = await model.get_user_by_id(user_id)
    access_token = auth.create_token(user_id, user["email"])
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


@router.post("/auth/resend-verification")
async def resend_verification(
    req: auth.LoginRequest,
    request: Request,
):
    """Resend verification email. Requires email+password to prevent abuse."""
    user = await model.get_user_by_email(req.email)
    # OIDC-only users have no password hash; treat that as invalid
    # credentials rather than letting verify_password crash on None.
    if (
        user is None
        or not user.get("password_hash")
        or not auth.verify_password(req.password, user["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("verified"):
        raise HTTPException(status_code=400, detail="Account already verified")

    # Rate limit: one resend per email per minute
    now = time.time()
    prune_timestamps(resend_timestamps, RESEND_COOLDOWN_SECONDS, now)
    last = resend_timestamps.get(req.email, 0)
    if now - last < RESEND_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    resend_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    verification_token = auth.create_verification_token(user["id"])
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    await send_email(
        emailsvc.send_verification_email(req.email, verification_url),
        req.email,
        "verification email",
    )
    return {"status": "sent"}


class ForgotPasswordRequest(auth.BaseModel):
    email: str


reset_timestamps: dict[str, float] = {}
RESET_COOLDOWN_SECONDS = 60


@router.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Send a password reset email if the account exists."""
    user = await model.get_user_by_email(req.email)
    if user is None:
        # Don't reveal whether the email exists
        return {"status": "sent"}

    # Rate limit: one reset email per address per minute
    now = time.time()
    prune_timestamps(reset_timestamps, RESET_COOLDOWN_SECONDS, now)
    last = reset_timestamps.get(req.email, 0)
    if now - last < RESET_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    reset_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    reset_token = auth.create_password_reset_token(user["id"])
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    await send_email(
        emailsvc.send_password_reset_email(req.email, reset_url),
        req.email,
        "password reset email",
    )
    return {"status": "sent"}


class ResetPasswordRequest(auth.BaseModel):
    token: str
    password: str


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    """Reset password using a token from the reset email."""
    user_id = auth.decode_password_reset_token(req.token)
    if user_id is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )
    auth.validate_password_length(req.password)
    if user_id == model.AGENT_USER_ID:
        raise HTTPException(
            status_code=400,
            detail="Cannot set a password on the system agent user",
        )
    password_hash = auth.hash_password(req.password)
    await model.update_password(user_id, password_hash)
    # Auto-login after reset
    user = await model.get_user_by_id(user_id)
    if user is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="User not found")
    token = auth.create_token(user_id, user["email"])
    return {"status": "reset", "access_token": token}


@router.post("/auth/login", response_model=auth.TokenResponse)
async def login(req: auth.LoginRequest):
    if not oidc.password_login_allowed(get_settings()):
        raise HTTPException(
            status_code=403, detail="Password login is disabled"
        )
    return await auth.login(req)


class LocalLoginResponse(auth.BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


@router.post("/auth/local", response_model=LocalLoginResponse)
async def local_login(request: Request):
    """No-login single-user mode: mint a token for the seeded default
    user, no credentials accepted (#1374).

    Only available when ``KLANGK_AUTH_MODES=none``. The loopback bind
    (``KLANGK_LISTEN``) plus the nginx per-location ``allow 127.0.0.1``
    ACL keep this endpoint unreachable from workspace containers; the
    freely-issued Bearer token is kept as belt-and-suspenders CSRF
    defense on every subsequent request.

    As a second belt-and-suspenders layer (and to close the front-proxy
    bypass, where a loopback proxy in front of nginx makes every request
    appear to come from 127.0.0.1), the backend independently verifies
    the *effective* client is loopback via :func:`util.client_is_loopback`.
    """
    if not oidc.local_login_allowed(get_settings()):
        raise HTTPException(
            status_code=403,
            detail="Local login is not enabled (auth mode is not 'none')",
        )
    # Independent loopback check: trusts X-Real-IP/X-Forwarded-For only when
    # the immediate peer is itself a trusted (loopback) proxy, so it can't be
    # spoofed by a direct non-loopback caller. See util.client_is_loopback.
    if not client_is_loopback(
        request.headers, request.client.host if request.client else None
    ):
        raise HTTPException(
            status_code=403,
            detail="Local login requires a loopback client",
        )
    email = resolve_env_value("KLANGK_DEFAULT_USER", "admin@example.com")
    user = await model.get_user_by_email(email)
    if user is None:
        # seed_default_user() runs in the lifespan before the app serves
        # traffic, so this only triggers if seeding was bypassed.
        raise HTTPException(
            status_code=500,
            detail="Default user is not seeded",
        )
    token = auth.create_token(user["id"], user["email"])
    return LocalLoginResponse(access_token=token, email=user["email"])


@router.post("/auth/refresh", response_model=auth.TokenResponse)
async def refresh_token(request: Request):
    """Exchange a valid access token for a new one."""
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization[7:]
    return await auth.refresh_token(token)


class ChangePasswordRequest(auth.BaseModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password")
async def change_password(
    req: ChangePasswordRequest,
    user: dict = Depends(auth.get_current_user),
):
    """Change password. Requires current password."""
    stored = await model.get_user_by_email(user["email"])
    # OIDC-only users have no password; their credentials are managed
    # by their identity provider and must not crash on a NULL hash.
    if stored is not None and not stored.get("password_hash"):
        raise HTTPException(
            status_code=403,
            detail="Account is managed by your identity provider",
        )
    if stored is None or not auth.verify_password(
        req.current_password, stored["password_hash"]
    ):
        raise HTTPException(
            status_code=401, detail="Current password is incorrect"
        )
    auth.validate_password_length(req.new_password)
    password_hash = auth.hash_password(req.new_password)
    await model.update_password(user["id"], password_hash)
    return {"status": "updated"}


class ChangeEmailRequest(auth.BaseModel):
    email: str
    password: str


@router.post("/auth/change-email")
async def change_email(
    req: ChangeEmailRequest,
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    """Change email. Requires password. Marks account as unverified."""
    stored = await model.get_user_by_email(user["email"])
    # OIDC-only users have no password; their credentials are managed
    # by their identity provider and must not crash on a NULL hash.
    if stored is not None and not stored.get("password_hash"):
        raise HTTPException(
            status_code=403,
            detail="Account is managed by your identity provider",
        )
    if stored is None or not auth.verify_password(
        req.password, stored["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None and existing["id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Email already in use")
    await model.update_email(user["id"], req.email)
    # Mark as unverified and send verification email
    async with model.transaction() as db:
        await db.execute(
            "UPDATE users SET verified = 0 WHERE id = ?",
            (user["id"],),
        )

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    token = auth.create_verification_token(user["id"])
    url = f"{proto}://{hostname}{base_path}/#/verify?token={token}"
    await send_email(
        emailsvc.send_verification_email(req.email, url),
        req.email,
        "verification email",
    )
    return {"status": "updated", "needs_verification": True}


class ChangeHandleRequest(auth.BaseModel):
    handle: str
    password: str


@router.post("/auth/change-handle")
async def change_handle(
    req: ChangeHandleRequest,
    user: dict = Depends(auth.get_current_user),
):
    """Change the current user's handle. Requires password confirmation."""
    stored = await model.get_user_by_email(user["email"])
    # OIDC-only users have no password; their credentials are managed
    # by their identity provider and must not crash on a NULL hash.
    if stored is not None and not stored.get("password_hash"):
        raise HTTPException(
            status_code=403,
            detail="Account is managed by your identity provider",
        )
    if stored is None or not auth.verify_password(
        req.password, stored["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Password is incorrect")
    try:
        await model.set_user_handle(user["id"], req.handle)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await wshandler.refresh_user_handle(user["id"], req.handle)
    return {"status": "updated", "handle": req.handle}


@router.get("/auth/me")
async def get_me(user: dict = Depends(auth.get_current_user)):
    """Return the current user's profile."""
    full = await model.get_user_by_id(user["id"])
    if full is None:  # pragma: no cover — race between auth and lookup
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": full["id"], "email": full["email"], "handle": full["handle"]}


@router.post("/auth/logout")
async def logout(
    request: Request,
    user: dict = Depends(auth.get_current_user),
):
    # Logout only invalidates credentials -- it deliberately does NOT stop the
    # user's containers. Per #301/#1235 the idle timeout is the only thing
    # that stops containers (plus the explicit, admin-gated
    # ``shutdown_container`` command). Stopping on logout was a holdover from
    # the per-user-container era and destroyed service sessions that should
    # outlive any single user's login.
    # Blocklist the token so it can't be reused after logout
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        await auth.logout(authorization[7:])

    # If the user logged in via OIDC and the provider has logout_redirect
    # enabled, return the IdP logout URL so the frontend can redirect.
    result: dict = {"status": "ok"}
    db_user = await model.get_user_by_email(user["email"])
    if db_user and db_user.get("provider", "local") != "local":
        provider = oidc.get_provider(db_user["provider"])
        if provider:
            hostname, proto, base_path = derive_hosting_info(
                request.headers,
                request.client.host if request.client else None,
            )
            post_logout_uri = f"{proto}://{hostname}{base_path}/#/login"
            logout_url = await oidc.build_logout_url(provider, post_logout_uri)
            if logout_url:
                result["oidc_logout_url"] = logout_url
    return result


# --- Invitation endpoints ---


class AcceptInviteRequest(BaseModel):
    token: str
    password: str


@router.post("/auth/accept-invite")
async def accept_invite(req: AcceptInviteRequest):
    """Accept an invitation and create a verified account."""
    result = auth.decode_invitation_token(req.token)
    if result is None:
        raise HTTPException(
            status_code=400, detail="Invalid or expired invitation token"
        )
    invitation_id, email = result

    invitation = await model.get_invitation(invitation_id)
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=400, detail="Invitation is no longer valid"
        )

    auth.validate_password_length(req.password)

    existing = await model.get_user_by_email(email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="An account with this email already exists"
        )

    password_hash = auth.hash_password(req.password)
    user = await model.create_user(email, password_hash, verified=True)
    await model.mark_invitation_accepted(invitation_id)

    access_token = auth.create_token(user["id"], user["email"])
    return {"status": "accepted", "access_token": access_token}
