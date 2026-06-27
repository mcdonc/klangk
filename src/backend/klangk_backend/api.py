"""API route handlers for Klangk backend."""

import asyncio
import json
import logging
import os
import posixpath
import secrets
import shutil
from sqlalchemy.exc import IntegrityError as SAIntegrityError
import subprocess
import tempfile
import time
import uuid
import io

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from . import (
    acl,
    auth,
    container,
    emailsvc,
    files,
    oidc,
    plugins,
    podman,
    wshandler,
    model,
    workspaces,
)
from .model import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
)
from .util import (
    API_PREFIX,
    derive_hosting_info,
    resolve_env_secret,
    sanitize_disposition_name,
)

logger = logging.getLogger(__name__)

# Maximum upload size for file uploads and workspace imports (bytes).
# Default 500 MB; override via KLANGK_FILE_UPLOAD_SIZE_MAX (in bytes).
FILE_UPLOAD_SIZE_MAX = int(
    resolve_env_secret("KLANGK_FILE_UPLOAD_SIZE_MAX", str(500 * 1024 * 1024))
)

root_router = APIRouter()
router = APIRouter()


@root_router.get("/health")
async def health():
    return {"status": "ok"}


@root_router.get("/empty")
async def empty():
    """Return an empty page. Used as a lightweight OAuth callback landing URL
    so the popup doesn't need to boot the Flutter SPA."""
    return PlainTextResponse("")


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


@router.get("/version")
async def version():
    """Return build version info, plus loaded plugin metadata."""
    if version_file := os.environ.get("KLANGK_VERSION_FILE", ""):
        if os.path.isfile(version_file):
            with open(version_file) as f:
                info = json.load(f)
            info["plugins"] = plugins.plugin_list()
            return info
    return {
        "version": "dev",
        "commit": "unknown",
        "built_at": None,
        "plugins": plugins.plugin_list(),
    }


# --- Test/debug endpoints (only when KLANGK_TEST_MODE is set) ---

if resolve_env_secret("KLANGK_TEST_MODE"):  # pragma: no cover

    @router.get("/test/idle-timeout")
    async def get_idle_timeout(workspace_id: str | None = None):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": container.registry.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {"idle_timeout_seconds": container.IDLE_TIMEOUT_SECONDS}

    class SetIdleTimeoutRequest(BaseModel):
        seconds: int
        workspace_id: str | None = None

    @router.post("/test/set-idle-timeout")
    async def set_idle_timeout(body: SetIdleTimeoutRequest):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        seconds = body.seconds
        workspace_id = body.workspace_id
        if workspace_id:
            container.registry.set_workspace_idle_timeout(
                workspace_id, seconds
            )
        else:
            container.IDLE_TIMEOUT_SECONDS = seconds
            container.CHECK_INTERVAL_SECONDS = max(10, min(60, seconds // 3))
        return {"idle_timeout_seconds": seconds}

    @router.get("/test/workspace-token/{workspace_id}")
    async def get_workspace_token(workspace_id: str):
        """Return a workspace JWT for testing (test only)."""
        return {"token": auth.create_workspace_token(workspace_id)}

    @router.get("/test/browsers/{workspace_id}")
    async def get_browsers(workspace_id: str):
        """Return all active browser registrations for a workspace (test only)."""
        browsers = []
        for bid, (ws_id, sock) in container.registry._browsers.items():
            if ws_id == workspace_id:
                email = None
                if sock is not None:
                    conn = wshandler.state.connections.get(sock)
                    if conn:
                        email = conn.user.get("email")
                browsers.append({"browser_id": bid, "email": email})
        return browsers


async def _send_email(coro, recipient: str, kind: str = "email") -> None:
    """Await an email-sending coroutine, converting failures to 503."""
    try:
        await coro
    except Exception as e:
        logger.error("Failed to send %s to %s: %s", kind, recipient, e)
        raise HTTPException(
            status_code=503,
            detail=f"Unable to send {kind}. Please try again later.",
        ) from None


# --- Config endpoint ---

LOGIN_BANNER_TITLE = resolve_env_secret("KLANGK_LOGIN_BANNER_TITLE", "")
LOGIN_BANNER = resolve_env_secret("KLANGK_LOGIN_BANNER", "")


@router.get("/config")
async def get_config():
    config = {
        "registration_enabled": auth.registration_enabled(),
        "invitations_enabled": auth.invitations_enabled(),
        "login_banner_title": LOGIN_BANNER_TITLE,
        "login_banner": LOGIN_BANNER,
        "oidc_providers": oidc.list_providers(),
        "auth_modes": oidc.auth_modes(),
        "instance_id": container.INSTANCE_ID,
    }
    config.update(plugins.frontend_config())
    return config


# --- Auth endpoints ---


@router.post("/auth/register")
async def register(
    req: auth.RegisterRequest,
    request: Request,
):
    if not oidc.password_login_allowed():
        raise HTTPException(
            status_code=403,
            detail="Password registration is disabled",
        )
    if resolve_env_secret("KLANGK_TEST_MODE"):
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

    hostname, proto, base_path = derive_hosting_info(request.headers)
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
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified) VALUES (?, ?, ?, 0)",
            (user_id, req.email, password_hash),
        )
        logger.info("User inserted (uncommitted): %s", req.email)
        await _send_email(
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


def _prune_timestamps(
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


_resend_timestamps: dict[str, float] = {}
RESEND_COOLDOWN_SECONDS = 60


@router.post("/auth/resend-verification")
async def resend_verification(
    req: auth.LoginRequest,
    request: Request,
):
    """Resend verification email. Requires email+password to prevent abuse."""
    user = await model.get_user_by_email(req.email)
    if user is None or not auth.verify_password(
        req.password, user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("verified"):
        raise HTTPException(status_code=400, detail="Account already verified")

    # Rate limit: one resend per email per minute
    now = time.time()
    _prune_timestamps(_resend_timestamps, RESEND_COOLDOWN_SECONDS, now)
    last = _resend_timestamps.get(req.email, 0)
    if now - last < RESEND_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    _resend_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(request.headers)
    verification_token = auth.create_verification_token(user["id"])
    verification_url = (
        f"{proto}://{hostname}{base_path}/#/verify?token={verification_token}"
    )
    await _send_email(
        emailsvc.send_verification_email(req.email, verification_url),
        req.email,
        "verification email",
    )
    return {"status": "sent"}


class ForgotPasswordRequest(auth.BaseModel):
    email: str


_reset_timestamps: dict[str, float] = {}
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
    _prune_timestamps(_reset_timestamps, RESET_COOLDOWN_SECONDS, now)
    last = _reset_timestamps.get(req.email, 0)
    if now - last < RESET_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail="Please wait before requesting another email",
        )
    _reset_timestamps[req.email] = now

    hostname, proto, base_path = derive_hosting_info(request.headers)
    reset_token = auth.create_password_reset_token(user["id"])
    reset_url = (
        f"{proto}://{hostname}{base_path}/#/reset-password?token={reset_token}"
    )
    await _send_email(
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
    if not oidc.password_login_allowed():
        raise HTTPException(
            status_code=403, detail="Password login is disabled"
        )
    return await auth.login(req)


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

    hostname, proto, base_path = derive_hosting_info(request.headers)
    token = auth.create_verification_token(user["id"])
    url = f"{proto}://{hostname}{base_path}/#/verify?token={token}"
    await _send_email(
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
    await wshandler.state.logout_user(user["id"])
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
            hostname, proto, base_path = derive_hosting_info(request.headers)
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


class SendInviteRequest(BaseModel):
    email: str


@router.post("/admin/invitations")
async def send_invitation(
    req: SendInviteRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Send an invitation email (admin only)."""
    if not auth.invitations_enabled():
        raise HTTPException(status_code=403, detail="Invitations are disabled")

    auth.validate_email(req.email)

    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="A user with this email already exists"
        )

    pending = await model.get_pending_invitation_by_email(req.email)
    if pending is not None:
        raise HTTPException(
            status_code=400,
            detail="A pending invitation already exists for this email",
        )

    invitation = await model.create_invitation(req.email, admin["id"])
    token = auth.create_invitation_token(invitation["id"], req.email)

    hostname, proto, base_path = derive_hosting_info(request.headers)
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await _send_email(
        emailsvc.send_invitation_email(req.email, invite_url, admin["email"]),
        req.email,
        "invitation email",
    )

    return {
        "id": invitation["id"],
        "email": invitation["email"],
        "status": invitation["status"],
    }


@router.get("/admin/invitations")
async def list_invitations(
    admin: dict = Depends(acl.has_permission("admin")),
):
    """List all invitations (admin only)."""
    return await model.list_invitations()


@router.delete("/admin/invitations/{invitation_id}")
async def revoke_invitation(
    invitation_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Revoke a pending invitation (admin only)."""
    revoked = await model.revoke_invitation(invitation_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )
    return {"status": "revoked"}


@router.post("/admin/invitations/{invitation_id}/resend")
async def resend_invitation(
    invitation_id: str,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Resend an invitation email (admin only)."""
    invitation = await model.get_invitation(invitation_id)
    if invitation is None or invitation["status"] != "pending":
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or not pending",
        )

    token = auth.create_invitation_token(invitation["id"], invitation["email"])
    hostname, proto, base_path = derive_hosting_info(request.headers)
    invite_url = (
        f"{proto}://{hostname}{base_path}/#/accept-invite?token={token}"
    )

    await _send_email(
        emailsvc.send_invitation_email(
            invitation["email"], invite_url, admin["email"]
        ),
        invitation["email"],
        "invitation email",
    )

    return {"status": "resent"}


# --- OIDC endpoints ---


@router.get("/auth/oidc/{provider_id}/login")
async def oidc_login(
    provider_id: str,
    request: Request,
    cli_redirect: str | None = None,
):
    """Redirect to the OIDC IdP for authentication."""
    if not oidc.oidc_login_allowed():
        raise HTTPException(status_code=404, detail="OIDC not enabled")

    provider = oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    # Validate cli_redirect is localhost only
    if cli_redirect and not cli_redirect.startswith(
        ("http://localhost:", "http://127.0.0.1:")
    ):
        raise HTTPException(
            status_code=400, detail="cli_redirect must be localhost"
        )

    verifier, challenge = oidc.generate_pkce()
    state = secrets.token_urlsafe(32)

    hostname, proto, base_path = derive_hosting_info(request.headers)
    redirect_uri = f"{proto}://{hostname}{base_path}{API_PREFIX}/auth/oidc/{provider_id}/callback"

    auth_url = await oidc.build_auth_url(
        provider, redirect_uri, state, challenge
    )

    response = RedirectResponse(url=auth_url, status_code=302)
    # Store state + verifier + cli_redirect in a cookie
    cookie_value = json.dumps(
        {
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "cli_redirect": cli_redirect,
        }
    )
    response.set_cookie(
        key=f"oidc_{provider_id}",
        value=cookie_value,
        httponly=True,
        max_age=600,
        samesite="lax",
        path="/",
    )
    return response


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

    provider = oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    # Retrieve and validate state from cookie
    cookie_name = f"oidc_{provider_id}"
    cookie_raw = request.cookies.get(cookie_name)
    if not cookie_raw:
        raise HTTPException(
            status_code=400, detail="Missing OIDC state cookie"
        )

    try:
        cookie_data = json.loads(cookie_raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid OIDC state cookie"
        )

    if cookie_data.get("state") != state:
        raise HTTPException(status_code=400, detail="State mismatch")

    # Exchange code for tokens
    try:
        tokens = await oidc.exchange_code(
            provider,
            code,
            cookie_data["redirect_uri"],
            cookie_data["verifier"],
        )
    except httpx.HTTPStatusError as exc:
        logger.error("OIDC token exchange failed: %s", exc.response.text)
        raise HTTPException(
            status_code=502, detail="Token exchange failed"
        ) from None

    # Validate ID token
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="No ID token in response")

    try:
        claims = await oidc.validate_id_token(
            provider, id_token, access_token=tokens.get("access_token")
        )
    except Exception as exc:
        logger.error("OIDC ID token validation failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="ID token validation failed"
        ) from None

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(
            status_code=502,
            detail="ID token missing sub or email claim",
        )
    auth.validate_email(email)

    # Call the OIDC login hook (if configured). The hook can:
    # - raise an exception to reject the login (HTTP 403)
    # - return None to allow the login without group sync
    # - return a set of group names to allow login and sync groups
    try:
        hook_groups = await oidc.call_login_hook(
            provider, claims, email, tokens
        )
    except Exception:
        logger.exception("OIDC login hook failed for provider %s", provider)
        raise HTTPException(
            status_code=403,
            detail="Login denied by server policy",
        ) from None

    # Find or create user
    user = await model.get_user_by_external_id(provider_id, sub)
    if user is None:
        # Check for existing local user with same email
        existing = await model.get_user_by_email(email)
        if existing is not None:
            # Link OIDC identity to existing user
            await model.link_oidc_identity(existing["id"], provider_id, sub)
            user = existing
        else:
            # JIT provisioning
            user = await model.create_user(
                email=email,
                password_hash=None,
                verified=True,
                provider=provider_id,
                external_id=sub,
            )

    # Sync group memberships if the hook returned group names
    if hook_groups is not None:
        await oidc.sync_oidc_groups(user["id"], hook_groups)

    # Issue Klangk JWT
    access_token = auth.create_token(user["id"], email)

    # Clear the state cookie
    cli_redirect = cookie_data.get("cli_redirect")

    if cli_redirect:
        # CLI flow: redirect to the CLI's localhost server with the token
        redirect_url = f"{cli_redirect}?token={access_token}"
    else:
        # Web flow: redirect to the frontend with token in the hash
        hostname, proto, base_path = derive_hosting_info(request.headers)
        redirect_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/oidc-complete?token={access_token}"
        )

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
    return response


# --- Workspace endpoints ---


@router.get("/workspaces")
async def list_workspaces(user: dict = Depends(auth.get_current_user)):
    return await workspaces.list_workspaces(user["id"])


@router.get("/workspaces/shared")
async def list_shared_workspaces(user: dict = Depends(auth.get_current_user)):
    return await model.list_shared_workspaces(user["id"])


class CreateWorkspaceRequest(BaseModel):
    name: str
    image: str | None = None
    default_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.get("/images")
async def list_images(_user: dict = Depends(auth.get_current_user)):
    return {
        "default": container.IMAGE_NAME,
        "allowed": sorted(container.ALLOWED_IMAGES),
    }


# --- Volume management ---


@router.get("/volumes")
async def list_volumes(user: dict = Depends(auth.get_current_user)):
    volumes = await podman.list_volumes(
        f"klangk.instance={container.INSTANCE_ID}"
    )
    uid = user["id"]
    return [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
        }
        for v in volumes
        if (v.get("Labels") or {}).get("klangk.user-id") == uid
    ]


class CreateVolumeRequest(BaseModel):
    name: str


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    user: dict = Depends(auth.get_current_user),
):
    if await podman.inspect_volume(body.name) is not None:
        raise HTTPException(
            status_code=409, detail=f"Volume {body.name!r} already exists"
        )
    info = await podman.create_volume(
        body.name,
        {
            "klangk.managed": "true",
            "klangk.instance": container.INSTANCE_ID,
            "klangk.user-id": user["id"],
        },
    )
    return {"name": info["Name"], "created": info.get("CreatedAt", "")}


@router.delete("/volumes/{name}")
async def delete_volume(
    name: str, user: dict = Depends(auth.get_current_user)
):
    info = await podman.inspect_volume(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    labels = info.get("Labels") or {}
    if labels.get("klangk.instance") != container.INSTANCE_ID:
        raise HTTPException(
            status_code=404,
            detail="Volume not managed by this Klangk instance",
        )
    if labels.get("klangk.user-id") != user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Volume belongs to another user",
        )
    try:
        await podman.remove_volume(name)
    except podman.PodmanError as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404, detail="Volume not found"
            ) from None
        if e.status == 409:
            raise HTTPException(
                status_code=409, detail="Volume is in use"
            ) from None
        raise
    return {"status": "deleted"}


async def _workspace_resource(request: Request, user: dict) -> str:
    """Resource function for workspace-level permission checks."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


async def _admin_resource(request: Request, user: dict) -> str:  # noqa: ARG001
    """Resource function for admin operations (always checks /admin)."""
    return "/admin"


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspaceRequest, user: dict = Depends(auth.get_current_user)
):
    if body.image and body.image not in container.ALLOWED_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Image {body.image!r} is not allowed. "
            f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
        )
    if body.mounts:
        mount_err = container.validate_mounts(body.mounts)
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    try:
        ws = await workspaces.create_workspace(
            user["id"],
            body.name,
            image=body.image,
            default_command=body.default_command,
            mounts=body.mounts,
            env=body.env,
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    except OSError as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(e))
    # Grant owner full access via ACL
    resource = f"/workspaces/{ws['id']}"
    await model.add_acl_entry(
        resource, 0, ACTION_ALLOW, "*", PRINCIPAL_USER, user_id=user["id"]
    )

    # Create workspace role groups and their ACL entries.
    role_groups = {
        f"owners-{ws['id']}": ["*"],
        f"coders-{ws['id']}": [
            "terminal",
            "code-in-isolation",
            "spectate-on-shared-terminals",
            "files",
            "chat",
        ],
        f"collaborators-{ws['id']}": [
            "terminal",
            "code-in-isolation",
            "code-in-shared-terminals",
            "spectate-on-shared-terminals",
            "share-terminals",
            "files",
            "chat",
        ],
        f"spectators-{ws['id']}": [
            "terminal",
            "spectate-on-shared-terminals",
            "chat",
        ],
    }
    pos = 1
    for group_name, perms in role_groups.items():
        group = await model.create_group(
            group_name, description=f"{group_name} for workspace {ws['name']}"
        )
        for perm in perms:
            await model.add_acl_entry(
                resource,
                pos,
                ACTION_ALLOW,
                perm,
                PRINCIPAL_GROUP,
                group_id=group["id"],
            )
            pos += 1
        # Add the creator to the owners group.
        if group_name.startswith("owners-"):
            await model.add_user_to_group(user["id"], group["id"])

    return ws


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    image: str | None = None
    default_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    user: dict = Depends(acl.has_permission("edit", _workspace_resource)),
):
    fields = body.model_dump(exclude_unset=True)
    if "image" in fields and fields["image"] is not None:
        if fields["image"] not in container.ALLOWED_IMAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Image {fields['image']!r} is not allowed. "
                f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
            )
    if "mounts" in fields and fields["mounts"]:
        mount_err = container.validate_mounts(fields["mounts"])
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    updated = await model.update_workspace(
        workspace_id, workspace["user_id"], **fields
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if "default_command" in fields:
        workspaces.write_default_command(
            workspace["user_id"], workspace_id, fields["default_command"]
        )
    return {"status": "updated"}


class DuplicateWorkspaceRequest(BaseModel):
    name: str


@router.post("/workspaces/{workspace_id}/duplicate")
async def duplicate_workspace(
    workspace_id: str,
    body: DuplicateWorkspaceRequest,
    user: dict = Depends(acl.has_permission("create", _workspace_resource)),
):
    source = await model.get_workspace(workspace_id)
    if source is None:  # pragma: no cover — race after ACL check
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        ws = await workspaces.create_workspace(
            user["id"],
            body.name,
            image=source.get("image"),
            default_command=source.get("default_command"),
            mounts=source.get("mounts"),
            env=source.get("env"),
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    # Grant owner full access via ACL
    await model.add_acl_entry(
        f"/workspaces/{ws['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return ws


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("delete", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Prefer the live container_id from the registry (tracks the currently
    # running container) over the DB value (may be stale if the container
    # was already stopped by idle timeout).
    live_state = container.registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    # reset_workspace_state (below) stops the agent session and clears
    # shared state; the agent subprocess runs inside the container, so
    # stopping the container kills it either way.
    if cid:
        await container.registry.stop_and_remove_container(cid)
    await wshandler.reset_workspace_state(workspace_id)

    deleted = await workspaces.delete_workspace(
        workspace_id, workspace["user_id"]
    )
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Clean up ACL entries for this workspace
    await model.delete_acl_entries_for_resource(f"/workspaces/{workspace_id}")
    return {"status": "deleted"}


@router.post("/workspaces/{workspace_id}/restart")
async def restart_workspace(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("terminal", _workspace_resource)),
):
    """Restart a workspace container.

    Stops and removes the running container.  The next WebSocket
    connect will start a fresh one with the same workspace config.
    """
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    live_state = container.registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    if cid:
        await container.registry.stop_and_remove_container(cid)
    await wshandler.reset_workspace_state(workspace_id)
    return {"status": "restarted"}


# --- Workspace export/import endpoints ---


@router.get("/workspaces/{workspace_id}/export")
async def export_workspace(
    workspace_id: str,
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Export a workspace as a .tar.gz archive (admin only).

    The archive contains workspace.json (metadata) and the home
    directory tree under home/.
    """
    workspace = await model.get_workspace(workspace_id, admin["id"])
    if workspace is None:
        # Admin may not own the workspace — look it up without access check.
        workspace = await model.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    home_dir = workspaces.home_path(workspace["user_id"], workspace_id)
    ws_name = workspace["name"]

    metadata = workspaces.workspace_metadata(workspace)

    # Estimate uncompressed size for client progress display.
    estimated_size = 0
    if home_dir.exists():
        try:
            result = subprocess.run(
                ["du", "-sb", str(home_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                estimated_size = int(result.stdout.split()[0])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # fall back to 0

    # Stream the tarball using GNU tar piped to stdout. Uses the shared
    # _build_export_tar_args (workspaces.py), same as build_workspace_archive.
    # Symlinks are stored as symlinks (not dereferenced).
    _CHUNK_SIZE = 256 * 1024  # 256 KB read chunks

    async def _stream():
        tmpdir = tempfile.mkdtemp()
        try:
            # Write workspace.json to temp dir
            meta_file = os.path.join(tmpdir, "workspace.json")
            with open(meta_file, "w") as f:
                json.dump(metadata, f, indent=2)

            tar_args = workspaces._build_export_tar_args("-", tmpdir, home_dir)

            proc = await asyncio.create_subprocess_exec(
                *tar_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                while True:
                    chunk = await proc.stdout.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
                await proc.wait()
            finally:
                if proc.returncode is None:  # pragma: no cover
                    proc.kill()
                    await proc.wait()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    safe_name = sanitize_disposition_name(ws_name)
    # Rough estimate: gzip typically compresses to ~20% of original
    # for text-heavy home dirs (source code, dotfiles, configs).
    estimated_compressed = max(int(estimated_size * 0.2), 1)
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.tar.gz"',
            "X-Estimated-Size": str(estimated_compressed),
        },
    )


@router.post("/workspaces/import")
async def import_workspace(
    file: UploadFile,
    name: str | None = None,
    user: dict = Depends(auth.get_current_user),
):
    """Import a workspace from a .tar.gz archive.

    Creates a new workspace with metadata from workspace.json and
    extracts the home directory from the archive.
    """
    # Stream upload to a temp file — abort if over the configured limit.
    max_upload = FILE_UPLOAD_SIZE_MAX
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    total = 0
    try:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_upload:
                raise HTTPException(
                    status_code=413,
                    detail=f"Archive exceeds {max_upload // (1024 * 1024)} MB limit",
                )
            tmp.write(chunk)
        tmp.close()
    except HTTPException:
        os.unlink(tmp.name)
        raise
    except Exception:
        os.unlink(tmp.name)
        raise

    # Extract workspace.json from the archive using tar (fast, no Python parsing).
    ws = None
    try:
        result = subprocess.run(
            ["tar", "xzf", tmp.name, "-O", "workspace.json"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail="Archive missing workspace.json or is corrupt",
            )
        metadata = json.loads(result.stdout)

        ws_name = name or metadata.get("name")
        if not ws_name:
            raise HTTPException(
                status_code=400,
                detail="No workspace name in archive or request",
            )

        image = metadata.get("image")
        if image and image not in container.ALLOWED_IMAGES:
            image = None  # fall back to default

        mounts = metadata.get("mounts")
        if mounts and container.validate_mounts(mounts):
            mounts = None  # invalid mounts, drop them

        # Sanitize env: drop keys that could interfere with
        # container startup (KLANGK_*, LD_*, PATH, etc.)
        raw_env = metadata.get("env")
        if isinstance(raw_env, dict):
            blocked = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PATH"}
            env = {
                k: v
                for k, v in raw_env.items()
                if not k.startswith("KLANGK_") and k not in blocked
            }
        else:
            env = None

        try:
            ws = await workspaces.create_workspace(
                user["id"],
                ws_name,
                image=image,
                default_command=metadata.get("default_command"),
                mounts=mounts,
                env=env,
            )
        except SAIntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"A workspace named {ws_name!r} already exists",
            )

        # Grant owner full access via ACL (mirrors create_workspace). Without
        # this an imported workspace has no ACL entries, so even its owner is
        # denied terminal/exec/files access.
        await model.add_acl_entry(
            f"/workspaces/{ws['id']}",
            0,
            ACTION_ALLOW,
            "*",
            PRINCIPAL_USER,
            user_id=user["id"],
        )

        # Extract home directory using tar (much faster than Python tarfile
        # for archives with many members). --strip-components=1 removes
        # the "home/" prefix. Only extract if home/ exists in the archive.
        home_dir = workspaces.home_path(user["id"], ws["id"])
        home_dir.mkdir(parents=True, exist_ok=True)
        check = subprocess.run(
            ["tar", "tzf", tmp.name, "home/"],
            capture_output=True,
            timeout=30,
        )
        if check.returncode == 0:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "tar",
                    "xzf",
                    tmp.name,
                    "--strip-components=1",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "-C",
                    str(home_dir),
                    "home/",
                ],
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                await workspaces.delete_workspace(ws["id"], user["id"])
                raise HTTPException(
                    status_code=400,
                    detail="Failed to extract home directory from archive",
                )

    except HTTPException:
        raise
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        if ws:
            await workspaces.delete_workspace(ws["id"], user["id"])
        raise HTTPException(
            status_code=400, detail="Invalid or corrupt archive"
        )
    finally:
        os.unlink(tmp.name)

    return ws


# --- Workspace sharing endpoints ---


async def _check_workspace_share(request: Request, user: dict) -> str:
    """Resource function for workspace share permission."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


async def _broadcast_workspace_members(workspace_id: str) -> None:
    """Push updated workspace members to all connected subscribers."""
    session = wshandler.state.get_session(workspace_id)
    if not session:
        return
    members = await model.get_workspace_members(workspace_id)
    workspace = await model.get_workspace(workspace_id)
    if workspace:
        owner = await model.get_user_by_id(workspace.get("user_id", ""))
        if owner and not any(m["id"] == owner["id"] for m in members):
            members.append({"id": owner["id"], "email": owner["email"]})
    session.broadcast({"type": "workspace_members", "members": members})


@router.get("/workspaces/{workspace_id}/members")
async def get_workspace_members(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    return await model.get_workspace_members(workspace_id)


class AddMemberRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str,
    body: AddMemberRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(
            status_code=400, detail="Cannot share with yourself"
        )
    # Add ACL entries granting the target user view+terminal on this workspace
    resource = f"/workspaces/{workspace_id}"
    existing = await model.get_acl_entries(resource)
    # Find the next available position
    max_pos = max((e["position"] for e in existing), default=-1)
    await model.add_acl_entry(
        resource,
        max_pos + 1,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await model.add_acl_entry(
        resource,
        max_pos + 2,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await model.add_acl_entry(
        resource,
        max_pos + 3,
        ACTION_ALLOW,
        "files",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await model.add_acl_entry(
        resource,
        max_pos + 4,
        ACTION_ALLOW,
        "chat",
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    await _broadcast_workspace_members(workspace_id)
    return {
        "status": "shared",
        "user_id": target["id"],
        "email": target["email"],
    }


@router.delete("/workspaces/{workspace_id}/members/{member_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    # Remove all ACL entries for this user on this workspace
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries(resource)
    remaining = [
        e
        for e in entries
        if not (
            e["principal_type"] == PRINCIPAL_USER and e["user_id"] == member_id
        )
    ]
    # Rewrite entries with new positions
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await model.replace_acl_entries(resource, remaining)
    await _broadcast_workspace_members(workspace_id)
    return {"status": "removed"}


ROLE_GROUP_SUFFIXES = ["owners", "coders", "collaborators", "spectators"]


@router.get("/workspaces/{workspace_id}/roles")
async def get_workspace_roles(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Return the workspace's role groups with their members."""
    roles = []
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            continue
        members = await model.get_group_members(group["id"])
        roles.append(
            {
                "role": suffix,
                "group_id": group["id"],
                "group_name": group_name,
                "members": [
                    {"id": m["id"], "email": m["email"]} for m in members
                ],
            }
        )
    return roles


class AddToRoleRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/roles/{role}")
async def add_to_workspace_role(
    workspace_id: str,
    role: str,
    body: AddToRoleRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Add a user to a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await model.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(target["id"], group["id"])
    return {"ok": True}


@router.delete("/workspaces/{workspace_id}/roles/{role}/{member_id}")
async def remove_from_workspace_role(
    workspace_id: str,
    role: str,
    member_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Remove a user from a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await model.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    await model.remove_user_from_group(member_id, group["id"])
    return {"ok": True}


class ChangeRoleRequest(BaseModel):
    email: str
    role: str | None = None  # None = remove from all roles


@router.patch("/workspaces/{workspace_id}/roles")
async def change_workspace_role(
    workspace_id: str,
    body: ChangeRoleRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Atomically change a user's workspace role.

    If ``role`` is set, removes the user from all other roles and adds
    them to the target role.  If ``role`` is null, removes the user
    from all roles.
    """
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None and body.role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role: {body.role}"
        )

    # Remove from all current roles
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            continue
        await model.remove_user_from_group(target["id"], group["id"])

    # Add to target role if specified
    if body.role is not None:
        group_name = f"{body.role}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="Role group not found")
        await model.add_user_to_group(target["id"], group["id"])

    return {"ok": True, "email": body.email, "role": body.role}


@router.get("/workspaces/{workspace_id}/groups")
async def get_workspace_groups(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Get groups with access to this workspace via ACL."""
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries_resolved(resource)
    seen = set()
    groups = []
    for e in entries:
        if e["principal_type"] == PRINCIPAL_GROUP and e.get("group_id"):
            gid = e["group_id"]
            if gid not in seen:
                seen.add(gid)
                groups.append({"id": gid, "name": e["principal"]})
    return groups


class AddGroupShareRequest(BaseModel):
    group_id: str


@router.post("/workspaces/{workspace_id}/groups")
async def add_workspace_group(
    workspace_id: str,
    body: AddGroupShareRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Share a workspace with a group (view/terminal/files/chat)."""
    group = await model.get_group_by_id(body.group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    resource = f"/workspaces/{workspace_id}"
    existing = await model.get_acl_entries(resource)
    max_pos = max((e["position"] for e in existing), default=-1)
    for i, perm in enumerate(["view", "terminal", "files", "chat"]):
        await model.add_acl_entry(
            resource,
            max_pos + 1 + i,
            ACTION_ALLOW,
            perm,
            PRINCIPAL_GROUP,
            group_id=body.group_id,
        )
    return {"status": "shared", "group_id": group["id"], "name": group["name"]}


@router.delete("/workspaces/{workspace_id}/groups/{group_id}")
async def remove_workspace_group(
    workspace_id: str,
    group_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Remove all ACL entries for a group on this workspace."""
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries(resource)
    remaining = [
        e
        for e in entries
        if not (
            e["principal_type"] == PRINCIPAL_GROUP
            and e["group_id"] == group_id
        )
    ]
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await model.replace_acl_entries(resource, remaining)
    return {"status": "removed"}


# --- Workspace ACL endpoints (for workspace owners/admins) ---


@router.get("/workspaces/{workspace_id}/acl")
async def get_workspace_acl(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Get resolved ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
    return await model.get_acl_entries_resolved(resource)


class WorkspaceAclEntry(BaseModel):
    action: int  # 0=deny, 1=allow
    principal_type: int  # 0=system, 1=user, 2=group
    permission: str
    user_id: str | None = None
    group_id: str | None = None
    system_principal: int | None = None


@router.put("/workspaces/{workspace_id}/acl")
async def replace_workspace_acl(
    workspace_id: str,
    entries: list[WorkspaceAclEntry],
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Replace all ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
    acl_entries = [
        {
            "position": i,
            "action": e.action,
            "principal_type": e.principal_type,
            "permission": e.permission,
            "user_id": e.user_id,
            "group_id": e.group_id,
            "system_principal": e.system_principal,
        }
        for i, e in enumerate(entries)
    ]
    await model.replace_acl_entries(resource, acl_entries)
    return await model.get_acl_entries_resolved(resource)


# --- User search endpoint ---


@router.get("/users/search")
async def search_users(
    q: str,
    _user: dict = Depends(auth.get_current_user),
):
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="Query too short")
    return await model.search_users(q)


# --- File endpoints ---


def _require_container(workspace_id: str) -> str:
    """Return the container_id for a running workspace, or raise 409."""
    state = container.registry.get_state(workspace_id)
    if state is None:
        raise HTTPException(status_code=409, detail="Container not running")
    state.record_activity()
    return state.container_id


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = "/",
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)
    try:
        return await files.list_files(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)
    try:
        content = await files.read_file(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(
            status_code=404, detail="File not found or too large"
        )
    return {"path": path, "content": content}


@router.delete("/workspaces/{workspace_id}/files")
async def delete_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)
    try:
        deleted = await files.delete_path(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": deleted, "status": "deleted"}


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    body: RenameFileRequest,
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)
    try:
        renamed = await files.rename_path(cid, body.old_path, body.new_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source not found")
    except FileExistsError:
        raise HTTPException(
            status_code=409, detail="Destination already exists"
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": renamed, "status": "renamed"}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)
    try:
        info = await files.stat_path(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if info is None:
        raise HTTPException(status_code=404, detail="Path not found")
    name = sanitize_disposition_name(posixpath.basename(path) or "download")
    if not info["is_dir"]:
        return StreamingResponse(
            files.stream_file(cid, path),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )
    return StreamingResponse(
        files.stream_dir_tar(cid, path),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.tar.gz"',
        },
    )


@router.post("/workspaces/{workspace_id}/files/upload")
async def upload_file(
    workspace_id: str,
    file: UploadFile,
    path: str = "",
    user: dict = Depends(acl.has_permission("files", _workspace_resource)),
):
    cid = _require_container(workspace_id)

    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:  # pragma: no cover
        raise HTTPException(status_code=400, detail="No filename provided")

    max_upload = FILE_UPLOAD_SIZE_MAX
    buf = io.BytesIO()
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_upload:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds {max_upload // (1024 * 1024)} MB limit",
            )
        buf.write(chunk)

    try:
        saved_path = await files.write_file(cid, filename, buf.getvalue())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}


# --- Browser bridge endpoint ---


async def _require_workspace_token(request: Request) -> str:
    """FastAPI dependency: validate workspace JWT from Authorization header.

    Returns the workspace_id. Raises 401 if missing, expired, or invalid.
    This duplicates the nginx auth_request check as defense-in-depth.
    """
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing workspace token")
    token = authorization[7:]
    result = auth.decode_workspace_token(token)
    if result is auth.WORKSPACE_TOKEN_EXPIRED:
        raise HTTPException(status_code=401, detail="Workspace token expired")
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid workspace token")
    return result


class BrowserDelegateRequest(BaseModel):
    model_config = {"extra": "allow"}
    action: str
    browser_id: str


def _resolve_bridge_target(body: BrowserDelegateRequest):
    """Resolve a browser ID to (session, target_sock, payload).

    Raises HTTPException (403/502) if the browser ID is unknown, the
    workspace has no session, or the target browser is not subscribed.
    """
    resolved = container.registry.resolve_browser(body.browser_id)
    if resolved is None:
        raise HTTPException(status_code=403, detail="Unknown browser ID")
    workspace_id, target_sock = resolved

    session = wshandler.state.get_session(workspace_id)
    if not session:
        raise HTTPException(
            status_code=502,
            detail="No browser client connected to this workspace",
        )

    if target_sock not in session.browser_subscribers:
        raise HTTPException(
            status_code=502,
            detail="Browser connection not available",
        )
    return session, target_sock, body.model_dump(exclude={"browser_id"})


@router.post("/browser-delegate")
async def browser_delegate(
    body: BrowserDelegateRequest,
    workspace_id: str = Depends(_require_workspace_token),
):
    """Bridge endpoint for container processes to delegate actions to the browser.

    The container reads the current browser ID via ``klangk-browser-id``
    and includes it in the POST.  The backend resolves the ID to the
    specific browser tab's WebSocket and relays the request.
    """
    session, target_sock, payload = _resolve_bridge_target(body)
    # Credential get operations may wait for user interaction (PAT dialog
    # or OAuth device flow) — allow up to 15 minutes (matching GitHub's
    # device code expiry).
    action = payload.get("action", "")
    operation = payload.get("operation", "")
    timeout = (
        900.0 if action == "git_credential" and operation == "get" else 30.0
    )
    result = await session.dispatch_browser_request_to(
        target_sock, payload, timeout=timeout
    )

    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/browser-delegate/stream")
async def browser_delegate_stream(
    body: BrowserDelegateRequest,
    workspace_id: str = Depends(_require_workspace_token),
):
    """Streaming bridge: relay browser output chunks back as NDJSON.

    For long-running actions (RAG + LLM), the browser pushes incremental
    browser_chunk messages and a terminal browser_response.  Each is streamed
    to the caller immediately, so there is no single bounded round-trip — the
    only limit is the per-chunk idle timeout.
    """
    session, target_sock, payload = _resolve_bridge_target(body)
    return StreamingResponse(
        session.dispatch_browser_request_stream_to(
            target_sock, payload, wshandler.bridge_idle_timeout()
        ),
        media_type="application/x-ndjson",
    )


# --- Container-to-chat API (workspace JWT auth) ---


class WorkspaceChatRequest(BaseModel):
    message: str


@router.post("/workspaces/post-chat-message")
async def workspace_chat(
    body: WorkspaceChatRequest,
    workspace_id: str = Depends(_require_workspace_token),
):
    """Post a chat message from a container using a workspace JWT.

    The message is stored as MSG_AGENT and broadcast to all connected
    WebSocket subscribers in the workspace.
    """
    workspace = await model.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    chat_msg = await model.add_chat_message(
        workspace_id,
        "agent",
        "agent",
        text,
        message_type=model.MSG_AGENT,
    )
    session = wshandler.state.get_session(workspace_id)
    if session:
        session.broadcast({"type": "chat_message", **chat_msg})
    return chat_msg


# --- Admin endpoints (require admin role) ---


@router.get("/admin/users")
async def list_users(admin: dict = Depends(acl.has_permission("admin"))):
    return await model.list_users()


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str | None = None
    send_verification_email: bool = False


@router.post("/admin/users")
async def admin_create_user(
    req: AdminCreateUserRequest,
    request: Request,
    admin: dict = Depends(acl.has_permission("admin")),
):
    """Create a user (admin only).

    By default creates a verified user with the given password.  When
    ``send_verification_email`` is true, the password field is ignored
    and a verification email is sent so the user can set their own
    password.
    """
    auth.validate_email(req.email)
    existing = await model.get_user_by_email(req.email)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Email already registered")

    if req.send_verification_email:
        user_id = str(uuid.uuid4())
        # Use a random password hash — the user will set their own
        # password via the verification link.
        password_hash = auth.hash_password(uuid.uuid4().hex[:24])

        hostname, proto, base_path = derive_hosting_info(request.headers)
        verification_token = auth.create_verification_token(user_id)
        verification_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/verify?token={verification_token}"
        )

        async with model.transaction() as db:
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified)"
                " VALUES (?, ?, ?, 0)",
                (user_id, req.email, password_hash),
            )
            await _send_email(
                emailsvc.send_verification_email(req.email, verification_url),
                req.email,
                "verification email",
            )

        return {
            "id": user_id,
            "email": req.email,
            "status": "pending_verification",
        }

    if not req.password:
        raise HTTPException(
            status_code=400,
            detail="Password is required when not sending verification email",
        )
    auth.validate_password_length(req.password)
    password_hash = auth.hash_password(req.password)
    user = await model.create_user(req.email, password_hash, verified=True)
    return {"id": user["id"], "email": user["email"], "status": "created"}


@router.delete("/admin/users/{user_id}")
async def delete_user(
    user_id: str, admin: dict = Depends(acl.has_permission("admin"))
):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    if user_id == model.AGENT_USER_ID:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the system agent user",
        )
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Stop all containers for this user before deleting
    await container.registry.stop_user_containers(user_id)
    # Archive workspace data before deletion
    await workspaces.archive_user_data(user_id, user["email"])
    deleted = await model.delete_user(user_id)
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted"}


class UpdateUserRequest(auth.BaseModel):
    email: str | None = None
    password: str | None = None
    handle: str | None = None


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if req.email is not None:
        await model.update_email(user_id, req.email)
    if req.password is not None:
        if user_id == model.AGENT_USER_ID:
            raise HTTPException(
                status_code=400,
                detail="Cannot set a password on the system agent user",
            )
        auth.validate_password_length(req.password)
        password_hash = auth.hash_password(req.password)
        await model.update_password(user_id, password_hash)
    if req.handle is not None:
        try:
            await model.set_user_handle(user_id, req.handle)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await wshandler.refresh_user_handle(user_id, req.handle)
    return {"status": "updated"}


@router.post("/admin/users/{user_id}/unlockout")
async def unlock_user(
    user_id: str, admin: dict = Depends(acl.has_permission("admin"))
):
    """Reset a user's login lockout so they can log in immediately."""
    user = await model.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.clear_login_attempts(user["email"])
    return {"status": "unlocked"}


# --- Group management endpoints ---


class CreateGroupRequest(BaseModel):
    name: str
    description: str | None = None


class UpdateGroupRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class AddGroupMemberRequest(BaseModel):
    user_id: str


# --- User-accessible group endpoints (ACL-gated per group) ---


async def _group_resource(request: Request, user: dict) -> str:  # noqa: ARG001
    """Resource function for group-level permission checks."""
    group_id = request.path_params.get("group_id")
    if group_id:
        return f"/groups/{group_id}"
    return "/groups"


@router.get("/groups")
async def user_list_groups(
    user: dict = Depends(auth.get_current_user),
):
    """List all groups (any authenticated user can see groups)."""
    return await model.list_groups()


@router.post("/groups")
async def user_create_group(
    req: CreateGroupRequest,
    user: dict = Depends(acl.has_permission("create", _group_resource)),
):
    """Create a group. The creator gets full ACL access."""
    existing = await model.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await model.create_group(req.name, req.description)
    # Grant creator full access via ACL
    await model.add_acl_entry(
        f"/groups/{group['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return group


@router.patch("/groups/{group_id}")
async def user_update_group(
    group_id: str,
    req: UpdateGroupRequest,
    user: dict = Depends(acl.has_permission("edit", _group_resource)),
):
    """Update a group (requires edit permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    updated = await model.update_group(
        group_id, name=req.name, description=req.description
    )
    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "updated"}


@router.delete("/groups/{group_id}")
async def user_delete_group(
    group_id: str,
    user: dict = Depends(acl.has_permission("delete", _group_resource)),
):
    """Delete a group (requires delete permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    await model.delete_group(group_id)
    await model.delete_acl_entries_for_resource(f"/groups/{group_id}")
    return {"status": "deleted"}


@router.get("/groups/{group_id}/members")
async def user_list_group_members(
    group_id: str,
    user: dict = Depends(acl.has_permission("view", _group_resource)),
):
    """List group members (requires view permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return await model.get_group_members(group_id)


@router.post("/groups/{group_id}/members")
async def user_add_group_member(
    group_id: str,
    req: AddGroupMemberRequest,
    user: dict = Depends(
        acl.has_permission("manage_members", _group_resource)
    ),
):
    """Add a member (requires manage_members permission on the group)."""
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    target = await model.get_user_by_id(req.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(req.user_id, group_id)
    return {"status": "added"}


@router.delete("/groups/{group_id}/members/{user_id}")
async def user_remove_group_member(
    group_id: str,
    user_id: str,
    user: dict = Depends(
        acl.has_permission("manage_members", _group_resource)
    ),
):
    """Remove a member (requires manage_members on the group)."""
    removed = await model.remove_user_from_group(user_id, group_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    return {"status": "removed"}


# --- Admin group endpoints (admin-only, kept for backward compat) ---


@router.get("/admin/groups")
async def list_groups(
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.list_groups()


@router.post("/admin/groups")
async def create_group(
    req: CreateGroupRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    existing = await model.get_group_by_name(req.name)
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A group with this name already exists"
        )
    group = await model.create_group(req.name, req.description)
    return group


@router.patch("/admin/groups/{group_id}")
async def update_group(
    group_id: str,
    req: UpdateGroupRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    updated = await model.update_group(
        group_id, name=req.name, description=req.description
    )
    if not updated:
        raise HTTPException(status_code=400, detail="No fields to update")
    return {"status": "updated"}


@router.delete("/admin/groups/{group_id}")
async def delete_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    await model.delete_group(group_id)
    return {"status": "deleted"}


@router.get("/admin/groups/{group_id}/members")
async def list_group_members(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return await model.get_group_members(group_id)


@router.post("/admin/groups/{group_id}/members")
async def add_group_member(
    group_id: str,
    req: AddGroupMemberRequest,
    admin: dict = Depends(acl.has_permission("admin")),
):
    group = await model.get_group_by_id(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    user = await model.get_user_by_id(req.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(req.user_id, group_id)
    return {"status": "added"}


@router.delete("/admin/groups/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    removed = await model.remove_user_from_group(user_id, group_id)
    if not removed:
        raise HTTPException(
            status_code=404, detail="User is not a member of this group"
        )
    return {"status": "removed"}


# --- ACL management endpoints ---


@router.get("/admin/acl/tree")
async def get_acl_tree(
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_tree_summary()


@router.get("/admin/acl/by-principal/user/{user_id}")
async def get_acl_by_user(
    user_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_entries_by_principal_user(user_id)


@router.get("/admin/acl/by-principal/group/{group_id}")
async def get_acl_by_group(
    group_id: str,
    admin: dict = Depends(acl.has_permission("admin")),
):
    return await model.get_acl_entries_by_principal_group(group_id)


@router.get("/admin/acl/resource")
async def get_resource_acl(
    resource: str,
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Get resolved ACL entries for any resource (admin only)."""
    return await model.get_acl_entries_resolved(resource)


@router.put("/admin/acl/resource")
async def replace_resource_acl(
    resource: str,
    entries: list[WorkspaceAclEntry],
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Replace ACL entries for any resource (admin only)."""
    # Validate: root ACL must keep Authenticated view access
    if resource == "/":
        has_auth_view = any(
            e.action == ACTION_ALLOW
            and e.principal_type == PRINCIPAL_SYSTEM
            and e.system_principal == SYSTEM_AUTHENTICATED
            and e.permission in ("view", "*")
            for e in entries
        )
        if not has_auth_view:
            raise HTTPException(
                status_code=400,
                detail="Root ACL must include Allow Authenticated view "
                "to prevent locking out all users",
            )

    # Validate: /admin ACL must keep admin group access
    if resource == "/admin":
        has_admin_group = any(
            e.action == ACTION_ALLOW
            and e.principal_type == PRINCIPAL_GROUP
            and e.permission in ("*", "admin")
            for e in entries
        )
        if not has_admin_group:
            raise HTTPException(
                status_code=400,
                detail="Admin ACL must include at least one Allow "
                "group entry to prevent locking out all admins",
            )

    acl_entries = [
        {
            "position": i,
            "action": e.action,
            "principal_type": e.principal_type,
            "permission": e.permission,
            "user_id": e.user_id,
            "group_id": e.group_id,
            "system_principal": e.system_principal,
        }
        for i, e in enumerate(entries)
    ]
    await model.replace_acl_entries(resource, acl_entries)
    return await model.get_acl_entries_resolved(resource)


STATIC_RESOURCES = [
    "/",
    "/workspaces",
    "/groups",
    "/admin",
    "/admin/users",
    "/admin/invitations",
    "/admin/groups",
]

ALL_PERMISSIONS = [
    "view",
    "create",
    "edit",
    "delete",
    "terminal",
    "code-in-isolation",
    "spectate-on-shared-terminals",
    "code-in-shared-terminals",
    "share-terminals",
    "files",
    "chat",
    "share",
    "manage_members",
    "admin",
    "manage_users",
    "manage_invitations",
    "*",
]


@router.get("/my-permissions")
async def my_permissions(
    request: Request,
    resource: str | None = None,
    user: dict = Depends(auth.get_current_user),
):
    """Return the current user's effective permissions.

    If ``resource`` query param is provided, checks permissions for that
    specific resource path (e.g., ``/workspaces/{id}``). Otherwise
    returns permissions for all static resources.
    """
    principals = await acl.get_principals(user["id"])
    groups = await model.get_user_groups(user["id"])
    if resource is not None:
        perms = [
            p
            for p in ALL_PERMISSIONS
            if await acl.check_permission(resource, principals, p)
        ]
        return {
            "user_id": user["id"],
            "email": user["email"],
            "groups": groups,
            "permissions": {resource: perms} if perms else {},
        }
    permissions = {}
    for res in STATIC_RESOURCES:
        perms = [
            p
            for p in ALL_PERMISSIONS
            if await acl.check_permission(res, principals, p)
        ]
        if perms:
            permissions[res] = perms
    return {
        "user_id": user["id"],
        "email": user["email"],
        "groups": groups,
        "permissions": permissions,
    }
