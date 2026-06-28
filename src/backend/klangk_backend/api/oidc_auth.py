"""OIDC login/callback routes.  (Submodule is named ``oidc_auth`` rather than ``oidc`` so ``api.oidc`` keeps resolving to the ``klangk_backend.oidc`` logic module that tests patch.)"""

import json
import logging
import secrets

import httpx
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import (
    RedirectResponse,
)

from .. import (
    auth,
    model,
    oidc,
)
from ..util import (
    API_PREFIX,
    derive_hosting_info,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _valid_cli_redirect(url: str | None) -> bool:
    """True if *url* is a permitted CLI redirect target (localhost only).

    The OIDC state cookie is unsigned and client-controlled, so the
    cli_redirect stored there must be re-validated at callback time —
    otherwise a tampered cookie could redirect the freshly-minted
    access token to an attacker-controlled host (#936).
    """
    return bool(url) and url.startswith(
        ("http://localhost:", "http://127.0.0.1:")
    )


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

    # Validate cli_redirect is localhost only (re-checked at callback,
    # since the state cookie storing it is unsigned — see #936).
    if cli_redirect and not _valid_cli_redirect(cli_redirect):
        raise HTTPException(
            status_code=400, detail="cli_redirect must be localhost"
        )

    verifier, challenge = oidc.generate_pkce()
    state = secrets.token_urlsafe(32)

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
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

    if _valid_cli_redirect(cli_redirect):
        # CLI flow: redirect to the CLI's localhost server with the token.
        # Re-validate here because the state cookie is unsigned and
        # client-controlled — a tampered cli_redirect must not leak the
        # access token to an arbitrary host (#936).
        redirect_url = f"{cli_redirect}?token={access_token}"
    else:
        # Web flow (also the safe fallback when the cookie's
        # cli_redirect was missing or tampered to a non-localhost host).
        hostname, proto, base_path = derive_hosting_info(
            request.headers, request.client.host if request.client else None
        )
        redirect_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/oidc-complete?token={access_token}"
        )

    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
    return response


# --- Workspace endpoints ---
