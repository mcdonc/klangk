"""OIDC client for external Identity Provider authentication."""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
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
    admin_claim: str | None = None
    admin_group: str | None = None


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
    KLANGK_OIDC_CONFIG. Returns empty list if not configured."""
    config_path = resolve_env_secret("KLANGK_OIDC_CONFIG", "")
    if not config_path or not os.path.isfile(config_path):
        return []

    with open(config_path) as f:
        raw = json.load(f)

    providers = []
    for entry in raw:
        secret = resolve_file_secret(entry.get("client_secret", ""))
        providers.append(
            OIDCProvider(
                id=entry["id"],
                display_name=entry["display_name"],
                issuer=entry["issuer"].rstrip("/"),
                client_id=entry["client_id"],
                client_secret=secret or "",
                scopes=entry.get("scopes", "openid email profile"),
                admin_claim=entry.get("admin_claim"),
                admin_group=entry.get("admin_group"),
            )
        )
    return providers


def init_providers() -> None:
    """Load providers into the module-level registry."""
    global _providers
    _providers = load_config()
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


# --- Discovery ---


async def discover(provider: OIDCProvider) -> dict:
    """Fetch OIDC discovery document, cached."""
    now = time.time()
    cached = _discovery_cache.get(provider.id)
    if cached and now - cached.fetched_at < _DISCOVERY_TTL:
        return cached.data

    url = f"{provider.issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            disc["token_endpoint"],
            data=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


# --- ID Token Validation ---


async def validate_id_token(provider: OIDCProvider, id_token: str) -> dict:
    """Validate and decode an ID token. Returns the claims dict."""
    jwks = await get_jwks(provider)
    # python-jose needs the keys in JWKS format
    claims = jose_jwt.decode(
        id_token,
        jwks,
        algorithms=["RS256", "ES256"],
        audience=provider.client_id,
        issuer=provider.issuer,
    )
    return claims


def extract_admin_role(provider: OIDCProvider, claims: dict) -> bool | None:
    """Check if the user should have the admin role based on IdP claims.

    Returns True (grant), False (revoke), or None (no mapping configured).
    """
    if not provider.admin_claim or not provider.admin_group:
        return None

    # Support dot-path claims like "realm_access.roles"
    value = claims
    for key in provider.admin_claim.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    if isinstance(value, list):
        return provider.admin_group in value
    if isinstance(value, str):
        return value == provider.admin_group
    return False


def clear_caches() -> None:
    """Clear all caches (for testing)."""
    _discovery_cache.clear()
    _jwks_cache.clear()
