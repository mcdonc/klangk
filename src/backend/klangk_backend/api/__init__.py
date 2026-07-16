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
    container,
    emailsvc,
    oidc,
    wshandler,
)
from ._common import autostart_allowed, get_app_dep

# Imported under an alias: the ``from . import auth as _auth_routes`` line
# below pulls in the api/auth.py *submodule*, and the import machinery writes
# the submodule into this package's __dict__ under the name ``auth`` — which
# is the same dict as this module's globals.  We rebind the bare ``auth``
# name to the logic module after the submodule imports (see below) so the
# instance endpoints reference klangk_backend.auth, not the route module.
from .. import auth as _auth_logic

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
async def version(app=Depends(get_app_dep)):
    """Return build version info, plus loaded plugin metadata."""
    if version_file := app.state.settings.version_file:
        if os.path.isfile(version_file):
            with open(version_file) as f:
                info = json.load(f)
            info["plugins"] = app.state.plugins.plugin_list()
            return info
    return {
        "version": "dev",
        "commit": "unknown",
        "built_at": None,
        "plugins": app.state.plugins.plugin_list(),
    }


# --- Test/debug endpoints (only when KLANGK_TEST_MODE is set) ---

if os.environ.get("KLANGK_TEST_MODE"):  # pragma: no cover

    @router.get("/test/idle-timeout")
    async def get_idle_timeout(
        workspace_id: str | None = None,
        app=Depends(get_app_dep),
    ):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": app.state.container_registry.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {
            "idle_timeout_seconds": app.state.container_registry.idle_timeout_seconds
        }

    class SetIdleTimeoutRequest(BaseModel):
        seconds: int
        workspace_id: str | None = None

    @router.post("/test/set-idle-timeout")
    async def set_idle_timeout(
        body: SetIdleTimeoutRequest,
        app=Depends(get_app_dep),
    ):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        seconds = body.seconds
        workspace_id = body.workspace_id
        if workspace_id:
            app.state.container_registry.set_workspace_idle_timeout(
                workspace_id, seconds
            )
        else:
            app.state.container_registry.set_idle_timeout(seconds)
        return {"idle_timeout_seconds": seconds}

    @router.get("/test/workspace-token/{workspace_id}")
    async def get_workspace_token(
        workspace_id: str,
        app=Depends(get_app_dep),
    ):
        """Return a workspace JWT for testing (test only)."""
        return {"token": app.state.auth.create_workspace_token(workspace_id)}

    @router.get("/test/browsers/{workspace_id}")
    async def get_browsers(
        workspace_id: str,
        app=Depends(get_app_dep),
    ):
        """Return all active browser registrations for a workspace (test only)."""
        browsers = []
        for bid, (
            ws_id,
            sock,
        ) in app.state.container_registry._browsers.items():
            if ws_id == workspace_id:
                email = None
                if sock is not None:
                    conn = app.state.sockets.connections.get(sock)
                    if conn:
                        email = conn.user.get("email")
                browsers.append({"browser_id": bid, "email": email})
        return browsers


@router.get("/config")
async def get_config(app=Depends(get_app_dep)):
    s = app.state.settings
    config = {
        "registration_enabled": app.state.auth.registration_enabled(),
        "invitations_enabled": app.state.auth.invitations_enabled(),
        # White-label product name (KLANGK_PRODUCT_NAME). Surfaced so the
        # frontend can rename the product (tab title, app-bar logo) without
        # a rebuild; defaults to "Klangk" for back-compat (#1149).
        "product_name": s.product_name,
        "login_banner_title": s.login_banner_title,
        "login_banner": s.login_banner,
        "login_banner_every_visit": s.login_banner_every_visit,
        "oidc_providers": app.state.oidc.list_providers(),
        "auth_modes": app.state.oidc.auth_modes(),
        "instance_id": app.state.util.instance_id(),
        # Whether per-workspace auto-start (start the container on server
        # boot) is permitted. The web UI gates its "Auto start" checkbox on
        # this so users can't toggle a setting the server will reject (#1115).
        "allow_autostart": autostart_allowed(app),
        # Surfaced so the UI can validate password length inline (matches
        # the rule enforced server-side by auth.validate_password_length).
        "min_password_length": app.state.auth.min_password_length,
        # Deployer logo override (KLANGK_LOGO_URL). Empty when unset, in
        # which case the frontend renders the default KlangkLogo widget.
        # Supports file:/cmd: resolution like other secrets. See #1152.
        "logo_url": s.logo_url,
        # Configurable legal & support links (#1177). Plain env values (no
        # file:/cmd: resolution -- they are public, shown pre-auth). Empty
        # string when unset; the frontend hides whatever isn't configured.
        "terms_url": s.terms_url,
        "privacy_url": s.privacy_url,
        "aup_url": s.aup_url,
        "support_url": s.support_url,
        "support_email": s.support_email,
    }
    config.update(app.state.plugins.frontend_config())
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
    principals = await request.app.state.acl.get_principals(user["id"])
    groups = await request.app.state.model.users.get_user_groups(user["id"])
    if resource is not None:
        permissions = await request.app.state.acl.permissions_for_resources(
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
    permissions = await request.app.state.acl.permissions_for_resources(
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
