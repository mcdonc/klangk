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

    hostname, proto, base_path = derive_hosting_info(
        request.headers, request.client.host if request.client else None
    )
    redirect_uri = f"{proto}://{hostname}{base_path}{API_PREFIX}/auth/oidc/{provider_id}/callback"

    auth_url = await oidc_inst.build_auth_url(
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

    if not isinstance(cookie_data, dict) or cookie_data.get("state") != state:
        raise HTTPException(status_code=400, detail="State mismatch")

    return cookie_data


async def _exchange_and_validate_token(oidc_inst, provider, code, cookie_data):
    """Exchange the authorization code for tokens and validate the ID token.

    Returns ``(claims, tokens)`` where *claims* contains at least ``sub``
    and ``email``.
    """
    try:
        tokens = await oidc_inst.exchange_code(
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

    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise HTTPException(
            status_code=502,
            detail="ID token missing sub or email claim",
        )

    return claims, tokens


async def _find_or_create_user(provider_id, sub, email):
    """Locate an existing user by OIDC identity or create one via JIT provisioning.

    Raises ``HTTPException(403)`` if the resolved user is the system agent —
    OIDC must never mint a session as the agent (#1225).
    """
    user = await model.get_user_by_external_id(provider_id, sub)
    if user is not None:
        if user["id"] == model.AGENT_USER_ID:
            raise HTTPException(
                status_code=403,
                detail="Cannot log in as the system agent",
            )
        return user

    existing = await model.get_user_by_email(email)
    if existing is not None:
        if existing["id"] == model.AGENT_USER_ID:
            raise HTTPException(
                status_code=403,
                detail="Cannot log in as the system agent",
            )
        await model.link_oidc_identity(existing["id"], provider_id, sub)
        return existing

    return await model.create_user(
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
        hostname, proto, base_path = derive_hosting_info(
            request.headers, request.client.host if request.client else None
        )
        redirect_url = (
            f"{proto}://{hostname}{base_path}"
            f"/#/oidc-complete?token={access_token}"
        )

    cookie_name = f"oidc_{provider_id}"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.delete_cookie(cookie_name, path="/")
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

    provider = request.app.state.oidc.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Unknown OIDC provider")

    cookie_data = _validate_state_cookie(request, provider_id, state)
    claims, tokens = await _exchange_and_validate_token(
        request.app.state.oidc, provider, code, cookie_data
    )

    email = claims["email"]
    auth.validate_email(email)

    # Require email_verified unless the operator trusts this IdP's
    # email claims (trust-email: true in the provider config).
    if not provider.trust_email and claims.get("email_verified") is not True:
        raise HTTPException(
            status_code=403,
            detail="Email not verified by identity provider",
        )

    # Call the OIDC login hook (if configured).
    try:
        hook_groups = await request.app.state.oidc.call_login_hook(
            provider, claims, email, tokens
        )
    except Exception:
        logger.exception("OIDC login hook failed for provider %s", provider)
        raise HTTPException(
            status_code=403,
            detail="Login denied by server policy",
        ) from None

    user = await _find_or_create_user(provider_id, claims["sub"], email)

    if hook_groups is not None:
        await oidc.sync_oidc_groups(user["id"], hook_groups)

    access_token = auth.create_token(user["id"], email)
    return _build_redirect_response(
        request, provider_id, access_token, cookie_data
    )


# --- Workspace endpoints ---
