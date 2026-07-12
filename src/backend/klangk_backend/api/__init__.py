"""API route handlers for the Klangk backend.

Historically every HTTP route — auth, workspaces, members, ACL, files,
admin, images, OIDC, browser-delegate, chat, imports/exports — lived in a
single ~2800-line ``api.py``.  That module has been split into per-domain
submodules, each mounting its own sub-router:

    _common.py         shared helpers / constants / request models
    auth.py            register/login/logout + password/email/handle changes
    oidc_auth.py       OIDC login + callback
    workspaces.py      CRUD + members + roles + groups + ACL + import/export
    files.py           file navigation / operations
    images.py          image + volume listing
    browser_delegate.py  browser bridge
    chat.py            container-to-chat
    admin.py           users / groups / invitations / ACL admin

This package builds the main ``router`` by including every sub-router
without a prefix (so route paths are unchanged) and keeps the unprefixed
``root_router`` for ``/health`` and ``/empty``.  The few endpoints that do
not belong to a single domain (version, config, my-permissions, and the
KLANGK_TEST_MODE-only endpoints) stay here.

It also re-exports the names external callers and tests depend on so
``from klangk_backend import api`` keeps working exactly as before:
``router`` and ``root_router`` (used by ``main.py`` and the test fixture),
the shared logic modules referenced as ``api.emailsvc`` / ``api.oidc`` /
``api.container`` / ``api.wshandler`` (patched by tests), and the auth
rate-limit globals (``api.resend_timestamps`` and friends).
"""

import json
import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import (
    acl,
    container,
    emailsvc,
    model,
    oidc,
    wshandler,
)
from ._common import get_app_state_dep

# Imported under an alias: the ``from . import auth as _auth_routes`` line
# below pulls in the api/auth.py *submodule*, and the import machinery writes
# the submodule into this package's __dict__ under the name ``auth`` — which
# is the same dict as this module's globals.  We rebind the bare ``auth``
# name to the logic module after the submodule imports (see below) so the
# instance endpoints reference klangk_backend.auth, not the route module.
from .. import auth as _auth_logic
from ..util import resolve_env_bool, resolve_env_value

# Route submodules, aliased because their names collide with the logic
# modules imported above (api/auth.py vs klangk_backend.auth, etc.) and we
# want the bare names to keep resolving to the logic modules.
from . import (
    admin as _admin_routes,
    auth as _auth_routes,
    browser_delegate as _browser_routes,
    chat as _chat_routes,
    files as _files_routes,
    images as _images_routes,
    oidc_auth as _oidc_routes,
    workspaces as _workspace_routes,
)
from .auth import (
    RESET_COOLDOWN_SECONDS,
    RESEND_COOLDOWN_SECONDS,
    prune_timestamps,
    reset_timestamps,
    resend_timestamps,
)

# ``klangk_backend.api.auth`` (the attribute) now points at the route
# submodule because of the import above; point the bare ``auth`` name back
# at the klangk_backend.auth logic module the instance endpoints use.
auth = _auth_logic

logger = logging.getLogger(__name__)

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


@router.get("/version")
async def version(app_state=Depends(get_app_state_dep)):
    """Return build version info, plus loaded plugin metadata."""
    if version_file := resolve_env_value("KLANGK_VERSION_FILE", ""):
        if os.path.isfile(version_file):
            with open(version_file) as f:
                info = json.load(f)
            info["plugins"] = app_state.plugins.plugin_list()
            return info
    return {
        "version": "dev",
        "commit": "unknown",
        "built_at": None,
        "plugins": app_state.plugins.plugin_list(),
    }


# --- Test/debug endpoints (only when KLANGK_TEST_MODE is set) ---

if resolve_env_value("KLANGK_TEST_MODE"):  # pragma: no cover

    @router.get("/test/idle-timeout")
    async def get_idle_timeout(
        workspace_id: str | None = None,
        app_state=Depends(get_app_state_dep),
    ):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": app_state.container_registry.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {
            "idle_timeout_seconds": app_state.container_registry.idle_timeout_seconds
        }

    class SetIdleTimeoutRequest(BaseModel):
        seconds: int
        workspace_id: str | None = None

    @router.post("/test/set-idle-timeout")
    async def set_idle_timeout(
        body: SetIdleTimeoutRequest,
        app_state=Depends(get_app_state_dep),
    ):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        seconds = body.seconds
        workspace_id = body.workspace_id
        if workspace_id:
            app_state.container_registry.set_workspace_idle_timeout(
                workspace_id, seconds
            )
        else:
            app_state.container_registry.set_idle_timeout(seconds)
        return {"idle_timeout_seconds": seconds}

    @router.get("/test/workspace-token/{workspace_id}")
    async def get_workspace_token(workspace_id: str):
        """Return a workspace JWT for testing (test only)."""
        return {"token": auth.create_workspace_token(workspace_id)}

    @router.get("/test/browsers/{workspace_id}")
    async def get_browsers(
        workspace_id: str,
        app_state=Depends(get_app_state_dep),
    ):
        """Return all active browser registrations for a workspace (test only)."""
        browsers = []
        for bid, (
            ws_id,
            sock,
        ) in app_state.container_registry._browsers.items():
            if ws_id == workspace_id:
                email = None
                if sock is not None:
                    conn = app_state.sockets.connections.get(sock)
                    if conn:
                        email = conn.user.get("email")
                browsers.append({"browser_id": bid, "email": email})
        return browsers


LOGIN_BANNER_TITLE = resolve_env_value("KLANGK_LOGIN_BANNER_TITLE", "")
LOGIN_BANNER = resolve_env_value("KLANGK_LOGIN_BANNER", "")
PRODUCT_NAME = resolve_env_value("KLANGK_PRODUCT_NAME", "Klangk") or "Klangk"

# Configurable legal & support links (#1177). These are PUBLIC URLs shown
# to unauthenticated users on the login/registration screens, in the app
# chrome, and in email footers -- so they deliberately use plain env values
# and do NOT go through resolve_env_value() (no file:/cmd: secret
# resolution). A deployer pointing these at sensitive internal resources
# would be exposing them to the world. Empty string when unset, matching the
# logo_url convention the frontend already falls back from.
TERMS_URL = resolve_env_value("KLANGK_TERMS_URL") or ""
PRIVACY_URL = resolve_env_value("KLANGK_PRIVACY_URL") or ""
AUP_URL = resolve_env_value("KLANGK_AUP_URL") or ""
SUPPORT_URL = resolve_env_value("KLANGK_SUPPORT_URL") or ""
SUPPORT_EMAIL = resolve_env_value("KLANGK_SUPPORT_EMAIL") or ""


@router.get("/config")
async def get_config(app_state=Depends(get_app_state_dep)):
    config = {
        "registration_enabled": auth.registration_enabled(),
        "invitations_enabled": auth.invitations_enabled(),
        # White-label product name (KLANGK_PRODUCT_NAME). Surfaced so the
        # frontend can rename the product (tab title, app-bar logo) without
        # a rebuild; defaults to "Klangk" for back-compat (#1149).
        "product_name": PRODUCT_NAME,
        "login_banner_title": LOGIN_BANNER_TITLE,
        "login_banner": LOGIN_BANNER,
        "oidc_providers": app_state.oidc.list_providers(),
        "auth_modes": app_state.oidc.auth_modes(),
        "instance_id": model.get_instance_id(),
        # Whether per-workspace auto-start (start the container on server
        # boot) is permitted. The web UI gates its "Auto start" checkbox on
        # this so users can't toggle a setting the server will reject (#1115).
        "allow_autostart": resolve_env_bool("KLANGK_ALLOW_AUTOSTART"),
        # Surfaced so the UI can validate password length inline (matches
        # the rule enforced server-side by auth.validate_password_length).
        "min_password_length": auth.MIN_PASSWORD_LENGTH,
        # Deployer logo override (KLANGK_LOGO_URL). Empty when unset, in
        # which case the frontend renders the default KlangkLogo widget.
        # Supports file:/cmd: resolution like other secrets. See #1152.
        "logo_url": resolve_env_value("KLANGK_LOGO_URL") or "",
        # Configurable legal & support links (#1177). Plain env values (no
        # file:/cmd: resolution -- they are public, shown pre-auth). Empty
        # string when unset; the frontend hides whatever isn't configured.
        "terms_url": TERMS_URL,
        "privacy_url": PRIVACY_URL,
        "aup_url": AUP_URL,
        "support_url": SUPPORT_URL,
        "support_email": SUPPORT_EMAIL,
    }
    config.update(app_state.plugins.frontend_config())
    return config


# --- Auth endpoints ---


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
        permissions = await acl.permissions_for_resources(
            [resource], principals, ALL_PERMISSIONS
        )
        perms = permissions.get(resource, [])
        return {
            "user_id": user["id"],
            "email": user["email"],
            "groups": groups,
            "permissions": {resource: perms} if perms else {},
        }
    # Batch all static resources into a single ACL query instead of
    # awaiting check_permission() per resource/permission pair.
    permissions = await acl.permissions_for_resources(
        STATIC_RESOURCES, principals, ALL_PERMISSIONS
    )
    return {
        "user_id": user["id"],
        "email": user["email"],
        "groups": groups,
        "permissions": permissions,
    }


# --- Mount per-domain sub-routers (no prefix: paths are already full) ---
router.include_router(_auth_routes.router)
router.include_router(_oidc_routes.router)
router.include_router(_workspace_routes.router)
router.include_router(_files_routes.router)
router.include_router(_images_routes.router)
router.include_router(_browser_routes.router)
router.include_router(_chat_routes.router)
router.include_router(_admin_routes.router)


__all__ = (
    "root_router",
    "router",
    # logic modules tests reference as api.<name>
    "emailsvc",
    "oidc",
    "container",
    "wshandler",
    # auth rate-limit state (see test_api.py)
    "prune_timestamps",
    "resend_timestamps",
    "reset_timestamps",
    "RESEND_COOLDOWN_SECONDS",
    "RESET_COOLDOWN_SECONDS",
)
