"""OIDC client for external Identity Provider authentication."""

import base64
import hashlib
import logging
import os
import secrets
import time

import yaml
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from jose import jwt as jose_jwt

from .util import resolve_env_secret, resolve_file_secret

logger = logging.getLogger(__name__)

# Cache TTL for OIDC discovery and JWKS (seconds)
_DISCOVERY_TTL = 300
_JWKS_TTL = 3600


@dataclass
class OIDCProvider:
    id: str
    display_name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    ca_cert: str | None = None  # path to CA cert PEM for custom trust
    token_validation_pem: str | None = None  # static RSA/EC public key PEM
    logout_redirect: bool = False  # redirect to IdP logout on user logout


@dataclass
class _CachedDiscovery:
    data: dict
    fetched_at: float


@dataclass
class _CachedJWKS:
    keys: dict
    fetched_at: float


_discovery_cache: dict[str, _CachedDiscovery] = {}
_jwks_cache: dict[str, _CachedJWKS] = {}

# Provider registry — loaded once at startup
_providers: list[OIDCProvider] = []


def load_config() -> list[OIDCProvider]:
    """Load OIDC provider config from the JSON file specified by
    KLANGK_OIDC_CONFIG. Returns empty list if not configured.
    Raises if the path is set but the file doesn't exist."""
    config_path = resolve_env_secret("KLANGK_OIDC_CONFIG", "")
    if not config_path:
        return []
    if not os.path.isfile(config_path):
        raise RuntimeError(
            f"KLANGK_OIDC_CONFIG={config_path!r} not found"
            " (use an absolute path)"
        )

    config_dir = os.path.dirname(os.path.abspath(config_path))
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    providers = []
    for entry in raw:
        secret = resolve_file_secret(entry.get("client_secret", ""))
        # Resolve ca_cert relative to the config file's directory
        ca_cert = entry.get("ca_cert")
        if ca_cert and not os.path.isabs(ca_cert):
            ca_cert = os.path.join(config_dir, ca_cert)
        providers.append(
            OIDCProvider(
                id=entry["id"],
                display_name=entry["display_name"],
                issuer=entry["issuer"].rstrip("/"),
                client_id=entry["client_id"],
                client_secret=secret or "",
                scopes=entry.get("scopes", "openid email profile"),
                ca_cert=ca_cert,
                token_validation_pem=entry.get("token_validation_pem"),
                logout_redirect=entry.get("logout_redirect", False),
            )
        )
    return providers


def init_providers() -> None:
    """Load providers into the module-level registry.

    Raises RuntimeError if KLANGK_AUTH_MODES requires OIDC but no
    providers are configured or the config file is missing.
    """
    global _providers
    _providers = load_config()
    mode = auth_modes()
    if mode in ("oidc", "both") and not _providers:
        raise RuntimeError(
            f"KLANGK_AUTH_MODES={mode!r} but no OIDC providers configured"
        )
    if _providers:
        names = ", ".join(p.id for p in _providers)
        logger.info("OIDC providers loaded: %s", names)


def get_provider(provider_id: str) -> OIDCProvider | None:
    """Look up a provider by ID."""
    return next((p for p in _providers if p.id == provider_id), None)


def list_providers() -> list[dict]:
    """Return public info for all configured providers."""
    return [{"id": p.id, "display_name": p.display_name} for p in _providers]


def is_enabled() -> bool:
    return len(_providers) > 0


def auth_modes() -> str:
    """Return the configured auth mode."""
    val = resolve_env_secret("KLANGK_AUTH_MODES", "")
    if val in ("oidc", "password", "both"):
        return val
    return "both" if is_enabled() else "password"


def password_login_allowed() -> bool:
    return auth_modes() in ("password", "both")


def oidc_login_allowed() -> bool:
    return auth_modes() in ("oidc", "both") and is_enabled()


# --- PKCE ---


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE verifier and challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# --- HTTP client ---


def _client_kwargs(provider: OIDCProvider) -> dict:
    """Return httpx.AsyncClient kwargs for a provider, including
    custom CA cert if configured."""
    kwargs: dict = {}
    if provider.ca_cert:
        kwargs["verify"] = provider.ca_cert
    return kwargs


# --- Discovery ---


async def discover(provider: OIDCProvider) -> dict:
    """Fetch OIDC discovery document, cached."""
    now = time.time()
    cached = _discovery_cache.get(provider.id)
    if cached and now - cached.fetched_at < _DISCOVERY_TTL:
        return cached.data

    url = f"{provider.issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(**_client_kwargs(provider)) as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

    _discovery_cache[provider.id] = _CachedDiscovery(data=data, fetched_at=now)
    return data


async def get_jwks(provider: OIDCProvider) -> dict:
    """Fetch JWKS keys for token validation, cached."""
    now = time.time()
    cached = _jwks_cache.get(provider.id)
    if cached and now - cached.fetched_at < _JWKS_TTL:
        return cached.keys

    disc = await discover(provider)
    jwks_uri = disc["jwks_uri"]
    async with httpx.AsyncClient(**_client_kwargs(provider)) as client:
        resp = await client.get(jwks_uri, timeout=10)
        resp.raise_for_status()
        keys = resp.json()

    _jwks_cache[provider.id] = _CachedJWKS(keys=keys, fetched_at=now)
    return keys


# --- Authorization URL ---


async def build_auth_url(
    provider: OIDCProvider,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the authorization URL to redirect the user to the IdP."""
    disc = await discover(provider)
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{disc['authorization_endpoint']}?{urlencode(params)}"


# --- Token Exchange ---


async def exchange_code(
    provider: OIDCProvider,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Exchange an authorization code for tokens."""
    disc = await discover(provider)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(**_client_kwargs(provider)) as client:
        resp = await client.post(
            disc["token_endpoint"],
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


# --- ID Token Validation ---


async def validate_id_token(
    provider: OIDCProvider,
    id_token: str,
    access_token: str | None = None,
) -> dict:
    """Validate and decode an ID token. Returns the claims dict.

    Uses the static token_validation_pem if configured, otherwise
    fetches JWKS from the IdP's discovery endpoint. Pass access_token
    so jose can verify the at_hash claim if present.
    """
    if provider.token_validation_pem:
        key = provider.token_validation_pem
    else:
        key = await get_jwks(provider)
    claims = jose_jwt.decode(
        id_token,
        key,
        algorithms=["RS256", "ES256"],
        audience=provider.client_id,
        issuer=provider.issuer,
        access_token=access_token,
    )
    return claims


async def build_logout_url(
    provider: OIDCProvider,
    post_logout_redirect_uri: str,
) -> str | None:
    """Build the IdP logout URL for RP-Initiated Logout.

    Returns None if logout_redirect is disabled or the IdP doesn't
    advertise an end_session_endpoint.
    """
    if not provider.logout_redirect:
        return None
    disc = await discover(provider)
    endpoint = disc.get("end_session_endpoint")
    if not endpoint:
        return None
    params = {
        "client_id": provider.client_id,
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    return f"{endpoint}?{urlencode(params)}"


def clear_caches() -> None:
    """Clear all caches (for testing)."""
    _discovery_cache.clear()
    _jwks_cache.clear()
