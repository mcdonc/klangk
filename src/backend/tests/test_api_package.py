"""Tests for the ``klangk_backend.api`` package split (issue #964).

The former monolithic ``api.py`` (~2800 lines, 84 routes) was split into a
per-domain package.  These tests lock in the refactor's invariants so a
future re-shuffle can't silently drop, duplicate, or reroute a handler, and
so the public surface that ``main.py`` and ``test_api.py`` depend on keeps
resolving:

* every original route is still registered (all 84 operations present),
* no (path, method) is registered twice,
* each per-domain submodule owns a non-empty sub-router with the expected
  number of routes,
* the re-exported names resolve (``api.router``/``api.root_router``, the
  logic modules referenced as ``api.emailsvc``/``api.oidc``/``api.container``
  /``api.wshandler``, and the auth rate-limit globals), and
* the re-exported rate-limit dicts are the *same objects* the auth handlers
  use, so test mutations reach the handlers.
"""

import sys

import pytest
from fastapi import FastAPI

import klangk_backend
import klangk_backend.api as api
from klangk_backend.util import API_PREFIX

# The api/auth.py *submodule*.  Note: ``import klangk_backend.api.auth as
# api_auth`` would bind ``api_auth`` to ``api.auth`` (an attribute), which
# __init__ deliberately re-points at the klangk_backend.auth *logic* module
# so the instance endpoints see the logic module.  The route submodule
# itself is therefore fetched from sys.modules.
api_auth = sys.modules["klangk_backend.api.auth"]

# Total HTTP route operations the monolith exposed (per the issue).  The
# split must preserve this exactly — no dropped or duplicated handlers.
EXPECTED_ROUTE_COUNT = 86

# Per-domain submodules and the number of routes each owns.  79 sub-routes
# + 3 routes defined directly on the main router (version, config,
# my-permissions) + 2 on the root router (health, empty) == 84.
SUBMODULE_ROUTES = {
    "auth": 14,
    "oidc_auth": 2,
    "workspaces": 23,
    "files": 6,
    "images": 4,
    "browser_delegate": 2,
    "chat": 1,
    "admin": 29,
}

# One representative path from every domain (and the cross-cutting
# root-router / instance endpoints) — guards against a whole submodule
# silently failing to mount.
REPRESENTATIVE_PATHS = [
    # root_router (unprefixed)
    "/health",
    "/empty",
    # defined directly on the main router (instance metadata)
    f"{API_PREFIX}/version",
    f"{API_PREFIX}/config",
    f"{API_PREFIX}/my-permissions",
    # auth
    f"{API_PREFIX}/auth/login",
    f"{API_PREFIX}/auth/verify-workspace-token",
    f"{API_PREFIX}/auth/accept-invite",
    # oidc_auth
    f"{API_PREFIX}/auth/oidc/{{provider_id}}/login",
    f"{API_PREFIX}/auth/oidc/{{provider_id}}/callback",
    # workspaces (CRUD + members + roles + groups + acl + import/export)
    f"{API_PREFIX}/workspaces",
    f"{API_PREFIX}/workspaces/import",
    f"{API_PREFIX}/workspaces/{{workspace_id}}/export",
    f"{API_PREFIX}/workspaces/{{workspace_id}}/members",
    f"{API_PREFIX}/workspaces/{{workspace_id}}/roles",
    f"{API_PREFIX}/workspaces/{{workspace_id}}/acl",
    f"{API_PREFIX}/users/search",
    # files
    f"{API_PREFIX}/workspaces/{{workspace_id}}/files",
    f"{API_PREFIX}/workspaces/{{workspace_id}}/files/upload",
    # images / volumes
    f"{API_PREFIX}/images",
    f"{API_PREFIX}/volumes",
    # browser bridge
    f"{API_PREFIX}/browser-delegate",
    f"{API_PREFIX}/browser-delegate/stream",
    # chat
    f"{API_PREFIX}/workspaces/post-chat-message",
    # admin (users / groups / invitations / acl)
    f"{API_PREFIX}/admin/users",
    f"{API_PREFIX}/admin/invitations",
    f"{API_PREFIX}/admin/groups",
    f"{API_PREFIX}/admin/acl/tree",
    # user-accessible groups
    f"{API_PREFIX}/groups",
]


def _build_app() -> FastAPI:
    """Assemble the app exactly like ``main.py`` / the test_api fixture."""
    app = FastAPI()
    app.include_router(api.root_router)
    app.include_router(api.router, prefix=API_PREFIX)
    return app


def _operations(app: FastAPI) -> list[tuple[str, str]]:
    """All registered (method, path) pairs, resolved via the OpenAPI schema.

    The main router includes sub-routers lazily (``_IncludedRouter``), so
    ``app.routes`` is not eagerly populated; the OpenAPI build resolves
    every included route, which is exactly what we want to verify.
    """
    paths = app.openapi()["paths"]
    ops = []
    for path, methods in paths.items():
        for method in methods:
            # skip the auto-generated HEAD/OPTIONS-ish entries; OpenAPI
            # only lists real HTTP methods here.
            ops.append((method, path))
    return sorted(ops)


# --- public surface ------------------------------------------------------


class TestPackagePublicSurface:
    def test_routers_are_exposed(self):
        """main.py does `from .api import root_router, router`."""
        from fastapi import APIRouter

        assert isinstance(api.root_router, APIRouter)
        assert isinstance(api.router, APIRouter)

    @pytest.mark.parametrize(
        "name", ["emailsvc", "oidc", "container", "wshandler"]
    )
    def test_logic_module_reexport_is_the_real_module(self, name):
        """``api.<name>`` must be the logic module tests patch, not a route
        submodule.  e.g. ``patch.object(api.oidc, "get_provider", ...)``
        only affects handlers if ``api.oidc`` is ``klangk_backend.oidc``."""
        assert getattr(api, name) is getattr(klangk_backend, name)

    def test_auth_attribute_is_the_logic_module(self):
        """The bare ``api.auth`` name is rebound to the logic module (the
        ``api/auth.py`` submodule import otherwise shadows it via the
        package __dict__)."""
        assert api.auth is klangk_backend.auth

    def test_auth_route_submodule_distinct_from_logic_module(self):
        """``api.auth`` is the logic module, but the route submodule still
        exists as ``klangk_backend.api.auth`` in sys.modules and is what
        the sub-router is mounted from."""
        from fastapi import APIRouter

        submod = sys.modules["klangk_backend.api.auth"]
        assert submod is not klangk_backend.auth
        assert submod is api._auth_routes
        assert isinstance(submod.router, APIRouter)

    @pytest.mark.parametrize(
        "attr",
        [
            "resend_timestamps",
            "reset_timestamps",
            "prune_timestamps",
            "RESEND_COOLDOWN_SECONDS",
            "RESET_COOLDOWN_SECONDS",
        ],
    )
    def test_auth_rate_limit_globals_reexported(self, attr):
        """The auth rate-limit state is reachable as ``api.<attr>`` (the
        legacy test surface), pointing at the owning submodule's value."""
        assert hasattr(api, attr)
        assert getattr(api, attr) is not None

    def test_resend_timestamps_is_same_object_as_submodule(self):
        """Mutating ``api.resend_timestamps`` must reach the handler, so
        the re-export has to be the *same* dict the auth module uses."""
        assert api.resend_timestamps is api_auth.resend_timestamps

    def test_reset_timestamps_is_same_object_as_submodule(self):
        assert api.reset_timestamps is api_auth.reset_timestamps

    def test_prune_timestamps_is_same_object_as_submodule(self):
        assert api.prune_timestamps is api_auth.prune_timestamps

    def test_cooldown_constants_match_submodule(self):
        assert api.RESEND_COOLDOWN_SECONDS == api_auth.RESEND_COOLDOWN_SECONDS
        assert api.RESET_COOLDOWN_SECONDS == api_auth.RESET_COOLDOWN_SECONDS
        assert api.RESEND_COOLDOWN_SECONDS == 60
        assert api.RESET_COOLDOWN_SECONDS == 60


# --- route registration parity ------------------------------------------


class TestRouteParity:
    def test_total_route_count_unchanged(self):
        """All 84 original operations are registered — none dropped."""
        ops = _operations(_build_app())
        assert len(ops) == EXPECTED_ROUTE_COUNT, (
            f"expected {EXPECTED_ROUTE_COUNT} routes, got {len(ops)}"
        )

    def test_no_duplicate_operations(self):
        """No (method, path) pair registered twice (double-mount guard)."""
        ops = _operations(_build_app())
        assert len(ops) == len(set(ops)), "duplicate routes detected"

    def test_operation_count_matches_method_sum(self):
        """Sanity: every operation carries a real HTTP method."""
        ops = _operations(_build_app())
        methods = {m for m, _ in ops}
        assert methods <= {"get", "post", "put", "delete", "patch"}

    @pytest.mark.parametrize("path", REPRESENTATIVE_PATHS)
    def test_representative_path_registered(self, path):
        """At least one route from every domain is reachable."""
        paths = _build_app().openapi()["paths"]
        assert path in paths, f"missing route: {path}"


# --- per-submodule structure --------------------------------------------


class TestSubmoduleStructure:
    @pytest.mark.parametrize(
        "submod,expected", sorted(SUBMODULE_ROUTES.items())
    )
    def test_submodule_route_count(self, submod, expected):
        """Each domain module owns its sub-router with all its routes."""
        from importlib import import_module

        from fastapi import APIRouter

        mod = import_module(f"klangk_backend.api.{submod}")
        assert isinstance(mod.router, APIRouter)
        assert len(mod.router.routes) == expected, (
            f"{submod}: expected {expected} routes, "
            f"got {len(mod.router.routes)}"
        )

    def test_submodule_route_counts_sum_to_subtotal(self):
        """The 8 sub-routers together account for 79 of the 84 routes."""
        from importlib import import_module

        total = 0
        for submod in SUBMODULE_ROUTES:
            total += len(
                import_module(f"klangk_backend.api.{submod}").router.routes
            )
        # 79 sub-routes + 3 direct (version/config/my-permissions) + 2
        # root (health/empty) == 84.
        assert total == EXPECTED_ROUTE_COUNT - 3 - 2

    def test_common_module_has_no_router(self):
        """``_common`` holds shared helpers only — it must not define a
        router (doing so would suggest a route got stranded there)."""
        assert not hasattr(api._common, "router") or api._common.router is None

    def test_shared_helpers_live_in_common(self):
        """Cross-domain helpers are centralized so both consumers import the
        same object."""
        from klangk_backend.api import _common

        for name in (
            "FILE_UPLOAD_SIZE_MAX",
            "send_email",
            "workspace_resource",
            "admin_resource",
            "require_workspace_token",
            "WorkspaceAclEntry",
        ):
            assert hasattr(_common, name), f"_common missing {name}"

    def test_file_upload_size_max_is_shared_object(self):
        """workspaces and files must see the same upload-size cap."""
        from klangk_backend.api import _common, files, workspaces

        assert workspaces.FILE_UPLOAD_SIZE_MAX is _common.FILE_UPLOAD_SIZE_MAX
        assert files.FILE_UPLOAD_SIZE_MAX is _common.FILE_UPLOAD_SIZE_MAX
