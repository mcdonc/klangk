"""OIDC client for external Identity Provider authentication."""

import asyncio
import base64
import hashlib
import importlib.util
import logging
import os
import secrets
import time
from collections.abc import Callable

import yaml
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from jose import jwt as jose_jwt

from . import model
from .exceptions import ConfigurationError
from .settings import get_settings
from .util import resolve_env_value, resolve_file_value

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
    trust_email: bool = False  # skip email_verified check for this IdP


@dataclass
class CachedDiscovery:
    data: dict
    fetched_at: float


@dataclass
class _CachedJWKS:
    keys: dict
    fetched_at: float


_discovery_cache: dict[str, CachedDiscovery] = {}
_jwks_cache: dict[str, _CachedJWKS] = {}

# Provider registry — loaded once at startup
_providers: list[OIDCProvider] = []


_SENTINEL = object()


def get(entry: dict, key: str, default: object = _SENTINEL) -> object:
    """Look up *key* (kebab-case) with snake_case fallback for backwards compat."""
    value = entry.get(key, _SENTINEL)
    if value is not _SENTINEL:
        return value
    snake = key.replace("-", "_")
    if snake != key:
        value = entry.get(snake, _SENTINEL)
        if value is not _SENTINEL:
            return value
    if default is not _SENTINEL:
        return default
    raise KeyError(key)


def _parse_providers(
    entries: list[dict], config_dir: str | None = None
) -> list[OIDCProvider]:
    """Parse a list of raw provider dicts into OIDCProvider objects.

    Shared by both inline (config-file ``oidc_providers:``) and external
    (``KLANGK_OIDC_CONFIG``) loading paths.  *config_dir* is used to resolve
    relative ``ca-cert`` paths — ``None`` when loaded inline (relative paths
    are not meaningful without a file to be relative to).
    """
    providers = []
    for entry in entries:
        secret = resolve_file_value(get(entry, "client-secret", ""))
        ca_cert = get(entry, "ca-cert", None)
        if ca_cert and config_dir and not os.path.isabs(ca_cert):
            ca_cert = os.path.join(config_dir, ca_cert)
        providers.append(
            OIDCProvider(
                id=entry["id"],
                display_name=get(entry, "display-name"),
                issuer=entry["issuer"].rstrip("/"),
                client_id=get(entry, "client-id"),
                client_secret=secret or "",
                scopes=entry.get("scopes", "openid email profile"),
                ca_cert=ca_cert,
                token_validation_pem=get(entry, "token-validation-pem", None),
                logout_redirect=get(entry, "logout-redirect", False),
                trust_email=get(entry, "trust-email", False),
            )
        )
    return providers


def load_config() -> list[OIDCProvider]:
    """Load OIDC provider config.

    Sources (checked in order, first non-empty wins):

    1. **External file** — ``KLANGK_OIDC_CONFIG`` env var pointing at a
       separate YAML file.  This is an env-var override, so it wins over
       the config file (consistent with the global precedence rule:
       env > file > defaults).
    2. **Inline** — ``oidc_providers:`` list in the klangkd config file
       (via :class:`~klangk_backend.settings.KlangkSettings`).

    Returns an empty list if neither is configured.  Raises
    :class:`~klangk_backend.exceptions.ConfigurationError` if the external
    file path is set but doesn't exist.
    """
    # 1. External file via KLANGK_OIDC_CONFIG (env override wins)
    config_path = resolve_env_value("KLANGK_OIDC_CONFIG", "")
    if config_path:
        if not os.path.isfile(config_path):
            raise ConfigurationError(
                f"KLANGK_OIDC_CONFIG={config_path!r} not found"
                " (use an absolute path)"
            )
        config_dir = os.path.dirname(os.path.abspath(config_path))
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        return _parse_providers(raw, config_dir=config_dir)

    # 2. Inline providers from the config file
    settings = get_settings()
    if settings.oidc_providers:
        return _parse_providers(settings.oidc_providers)

    return []


def init_providers() -> None:
    """Load providers into the module-level registry.

    Raises ConfigurationError if KLANGK_AUTH_MODES requires OIDC but no
    providers are configured or the config file is missing.
    """
    global _providers
    _providers = load_config()
    mode = auth_modes()
    if mode in ("oidc", "both") and not _providers:
        raise ConfigurationError(
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
    """Return the configured auth mode.

    One of ``password``, ``oidc``, ``both``, or ``none`` (no-login
    single-user local-dev mode). ``none`` auto-issues a token for the
    seeded default user via ``POST /api/v1/auth/local``; see #1374.

    Resolution order when ``KLANGK_AUTH_MODES`` is unset:

    1. ``KLANGK_PRESET`` (#1397) — a ``*-auth`` preset defaults the mode
       to ``password`` (the gate is required by the preset; the backend
       defaults to password). A ``*-noauth`` preset defaults to ``none``.
    2. otherwise, legacy behaviour: ``none`` unless an OIDC provider is
       configured — configuring OIDC is the signal that real multi-user
       auth is wanted (in which case the default is ``both``).

    A fresh klangk with nothing configured therefore boots in no-login
    single-user mode, bound to loopback, and "just works" locally without a
    password. Operators who want the old password-default behaviour (or any
    other mode) set ``KLANGK_AUTH_MODES`` explicitly. A preset that requires
    a different backend (e.g. OIDC) sets ``KLANGK_AUTH_MODES=oidc`` — the
    preset only owns the *default*, never an override.
    """
    val = resolve_env_value("KLANGK_AUTH_MODES", "")
    if val in ("oidc", "password", "both", "none"):
        return val
    # ``KLANGK_AUTH_MODES`` unset — let the deployment preset (#1397) own the
    # default before falling back to the legacy OIDC-promotion rule. A ``*-auth``
    # preset means the gate is required, so default to password; the preset
    # never overrides an explicit ``KLANGK_AUTH_MODES`` (handled above). The
    # OIDC-promotion fallback below is slated for removal in #1392 chunk 7
    # ("OIDC presence should not change auth_mode") and lives only for
    # backward compat with pre-#1397 operators.
    preset = get_settings().preset
    if preset is not None and preset.endswith("-auth"):
        return "password"
    if is_enabled():
        return "both"
    return "none"


def password_login_allowed() -> bool:
    return auth_modes() in ("password", "both")


def local_login_allowed() -> bool:
    """True when no-login single-user mode is active (``none``)."""
    return auth_modes() == "none"


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


def client_kwargs(provider: OIDCProvider) -> dict:
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
    async with httpx.AsyncClient(**client_kwargs(provider)) as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

    _discovery_cache[provider.id] = CachedDiscovery(data=data, fetched_at=now)
    return data


async def get_jwks(provider: OIDCProvider) -> dict:
    """Fetch JWKS keys for token validation, cached."""
    now = time.time()
    cached = _jwks_cache.get(provider.id)
    if cached and now - cached.fetched_at < _JWKS_TTL:
        return cached.keys

    disc = await discover(provider)
    jwks_uri = disc["jwks_uri"]
    async with httpx.AsyncClient(**client_kwargs(provider)) as client:
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
    async with httpx.AsyncClient(**client_kwargs(provider)) as client:
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


# --- OIDC login hook ---

_login_hook: Callable | None = None
_login_hook_is_async: bool = False


def _parse_hook_value(raw: str) -> tuple[str, str]:
    """Parse KLANGK_OIDC_LOGIN_HOOK into (file_path, func_name).

    Accepted formats:
    - ``/path/to/hook.py:func_name``
    - ``/path/to/hook.py``  (defaults to ``on_login``)
    """
    if ":" in raw:
        path, func_name = raw.rsplit(":", 1)
    else:
        path = raw
        func_name = "on_login"
    return path, func_name


def load_login_hook() -> None:
    """Load the OIDC login hook from KLANGK_OIDC_LOGIN_HOOK.

    The value is a file path to a Python script, optionally followed
    by ``:func_name``.  If the function name is omitted it defaults
    to ``on_login``.  The file is loaded directly via
    ``importlib.util`` — it does **not** need to be on ``PYTHONPATH``.

    The hook is called after ID token validation and before user
    provisioning.  It combines login validation and group mapping:

    - **Raise** any exception → login rejected (HTTP 403, message
      from the exception).
    - **Return** ``None`` → login allowed, no group sync.
    - **Return** a ``set[str]`` of group names → login allowed,
      memberships synced to those groups.

    If not set, all OIDC logins are accepted with no group sync.
    """
    global _login_hook, _login_hook_is_async
    raw = resolve_env_value("KLANGK_OIDC_LOGIN_HOOK")
    if not raw:
        _login_hook = None
        _login_hook_is_async = False
        return
    path, func_name = _parse_hook_value(raw)
    if not os.path.isfile(path):
        raise ConfigurationError(
            f"KLANGK_OIDC_LOGIN_HOOK: file not found: {path!r}"
        )
    spec = importlib.util.spec_from_file_location("_klangk_login_hook", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ConfigurationError(
            f"KLANGK_OIDC_LOGIN_HOOK: could not load: {path!r}"
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hook = getattr(mod, func_name, None)
    if hook is None or not callable(hook):
        raise ConfigurationError(
            f"KLANGK_OIDC_LOGIN_HOOK: {func_name!r} not found or not "
            f"callable in {path!r}"
        )
    _login_hook = hook
    _login_hook_is_async = asyncio.iscoroutinefunction(hook)
    logger.info("OIDC login hook loaded: %s", raw)


async def call_login_hook(
    provider: OIDCProvider,
    claims: dict,
    email: str,
    tokens: dict,
) -> set[str] | None:
    """Call the OIDC login hook.

    Returns group names to sync, or None if no groups.
    Raises the hook's exception if login is rejected.
    If no hook is configured, returns None (login allowed, no sync).
    """
    if _login_hook is None:
        return None
    if _login_hook_is_async:
        result = await _login_hook(provider, claims, email, tokens)
    else:
        result = _login_hook(provider, claims, email, tokens)
    if result is None:
        return None
    return set(result)


async def sync_oidc_groups(
    user_id: str,
    groups: set[str],
) -> None:
    """Sync group memberships from the login hook result."""
    # Resolve group names to IDs, auto-creating missing groups
    desired_ids: set[str] = set()
    for name in groups:
        group = await model.get_group_by_name(name)
        if group is None:
            group = await model.create_group(name)
            logger.info("Auto-created group %r from OIDC hook", name)
        desired_ids.add(group["id"])

    # Diff against current oidc_sync memberships
    current_ids = set(await model.get_user_oidc_sync_group_ids(user_id))
    for gid in desired_ids - current_ids:
        await model.add_user_to_group(user_id, gid, source="oidc_sync")
    for gid in current_ids - desired_ids:
        await model.remove_user_from_group(user_id, gid)


def clear_caches() -> None:
    """Clear all caches (for testing)."""
    global _login_hook, _login_hook_is_async
    _discovery_cache.clear()
    _jwks_cache.clear()
    _login_hook = None
    _login_hook_is_async = False
