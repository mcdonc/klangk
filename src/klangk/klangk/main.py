"""Klangk backend: FastAPI app composition root.

The app is assembled in :func:`build_app` below; the sub-concerns that
used to live in this module were split out in the #2738 audit/refactor:

- :mod:`klangk.lifecycle` — ``Lifecycle`` (startup/shutdown/restart +
  seeding), the ``lifespan`` context manager, ``setup_logfire``.
- :mod:`klangk.middleware` — ``LiveCORSMiddleware``, ``InFlightRequests``,
  ``InFlightMiddleware``.
- :mod:`klangk.static` — static file mounts + the no-cache middleware.
- :mod:`klangk.bind_safety` — the no-auth loopback-bind gate.

The names above are re-exported here so existing callers (tests,
``klangkd``) that reach them as ``klangk.main.<name>`` keep working.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from . import (
    acl,
    auth,
    container,
    consent,
    emailsvc,
    files,
    server_schedule,
    inactivity,
    model,
    caddy as caddy_mod,
    oidc,
    features,
    nix,
    podman,
    netfilter,
    sidecar_connections,
    ssl_trust,
    terminal,
    util as util_mod,
    workspaces,
    wshandler,
)
from .llm_router import LLMRouter
from .wshandler import (
    handle_consent_decider,
    handle_egress_sidecar,
    handle_websocket,
)
from .settings import KlangkSettings
from .logger import configure as configure_logging
from .api import root_router, router
from .util import API_PREFIX
from .lifecycle import Lifecycle, lifespan
from .middleware import (
    InFlightMiddleware,
    InFlightRequests,
    LiveCORSMiddleware,
)
from .static import no_cache_headers, setup_static_files

logger = logging.getLogger(__name__)


async def agent_principal_error_handler(request, exc):  # noqa: ARG001
    """Reject any operation that would make the agent an ACL principal.

    Raised at the model choke points (``add_user_to_group``,
    ``add_acl_entry``, ``delete_user``, ``update_password``); translated
    to HTTP 400 here so route handlers carry no per-endpoint guard code.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def register_exception_handlers(application: FastAPI) -> None:
    """Register global exception handlers on a FastAPI application.

    Called for the production app (in :func:`build_app`) and by the test
    app fixture so both surface the same handler wiring without
    duplicating the handler.
    """
    application.add_exception_handler(
        model.AgentPrincipalError, agent_principal_error_handler
    )


def build_app(settings: KlangkSettings) -> FastAPI:
    """Single composition root (#1426).

    Constructs the FastAPI app, wires middleware, routers, exception
    handlers, the WebSocket endpoint, and static files. The ASGI app is the
    *only* global; everything else is reached per-request via
    :func:`get_app_dep` (or ``app.state`` for non-request code).
    """
    app = FastAPI(title="Klangk", lifespan=lifespan)
    app.state.settings = settings
    # #2527: in-flight HTTP request counter for the SIGHUP quiesce phase.
    # Created before any middleware/route wiring; InFlightMiddleware wraps
    # every request through this shared instance.
    app.state.inflight_requests = InFlightRequests()
    # #1467: logging is configured centrally (module-level defaults are
    # already active from the import of klangk.logger; this call re-applies
    # the level from KLANGKD_LOG_LEVEL now that settings are finalized, and
    # is also the SIGHUP reconfigure path). No app.state.logger object —
    # logging is global module state, reconfigured at this explicit seam.
    configure_logging(settings)
    # #1501: Auth(app_state) owns every auth config value and JWT
    # operation (previously module-level globals + import-time
    # resolve_env_value reads in auth.py). Reads self.settings at
    # construction/call time.
    # #1567: SSLTrust(app_state) owns the settings-dependent trust surface
    # (cert-dir resolver + backend-process trust applier). The 4 pure
    # path/bundle helpers stay module-level in ssl_trust.py.
    app.state.ssl_trust = ssl_trust.SSLTrust(app)
    # #1365: NetFilter(app_state) owns the egress-filter settings surface
    # (validators, host-resolver detection, the deploy default allow-list).
    # Enforcement lives in the network sidecar (#2255). Reaches config
    # through self.settings.
    app.state.netfilter = netfilter.NetFilter(app)
    app.state.auth = auth.Auth(app)
    # #1468: Podman(settings) owns the resolved binary path + the ~20 CLI
    # wrappers. Constructed before the registry/terminal so they reach it
    # via self.app.state.podman (#1426).
    app.state.podman = podman.Podman(app)
    # Slice 2c (#1475): the WebSocketState is an owned instance wired onto
    # app.state.sockets. Constructed before the registry so it reaches it
    # via self.app.state.sockets — no module-level singleton.
    app.state.sockets = wshandler.WebSocketState(app)
    # Slice 2 (#1449): the container registry is an owned instance, not a
    # module global. The lifespan reads app.state.container_registry.
    app.state.container_registry = container.ContainerRegistry(app)
    # #2242/#2311: egress-consent retention sweeper — prunes the
    # egress_consent table past the retention window / cap (#2303).
    # Consent events themselves arrive over the sidecar WS
    # (/ws/egress-sidecar) and are handled by the coordinator.
    app.state.consent_sweeper = consent.EgressConsentSweeper(app)
    # #2588: dormant-account sweeper — disables accounts (except the
    # agent and admin-group members) whose last activity is older than
    # KLANGKD_INACTIVITY_DISABLE_DAYS.
    app.state.inactivity_sweeper = inactivity.InactivitySweeper(app)
    # #2526: MemoryPressureEvictor — host memory-pressure eviction of idle
    # workspaces (sibling loop to the registry's IdleMonitor). Reads its
    # thresholds live off settings (SIGHUP-reloadable) via self.app.
    app.state.memory_evictor = container.eviction.MemoryPressureEvictor(app)
    # #2661: scheduled host shutdown/restart — persists schedules in the
    # DB (surviving daemon restarts), broadcasts the pending snapshot to
    # all clients, and fires due actions (drain workspaces, then the
    # configured KLANGKD_HOST_*_COMMAND).
    app.state.server_scheduler = server_schedule.ServerScheduler(app)
    app.state.consent_coordinator = consent.ConsentCoordinator(app)
    app.state.consent_deciders = consent.ConsentDeciderRegistry(app)
    # #2339: live network-sidecar sockets by workspace, so a revoke can push a
    # rule-drop to a workspace's sidecar + correlate the ack.
    app.state.sidecar_connections = sidecar_connections.SidecarConnections(
        app
    )
    # #2201: per-workspace nix store via a btrfs snapshot or fuse overlay
    # (off unless KLANGKD_NIX_SEED__PATH names a seed; see nix.Nix).
    app.state.nix = nix.Nix(app)
    # Slice 2b (#1463): proxy watchdog is an owned CaddyWatchdog instance
    # (Caddy is the sole reverse-proxy engine in 2.X, #1642) with
    # start()/stop()/reconfigure() methods called by the lifespan + SIGHUP.
    app.state.proxy_watchdog = caddy_mod.CaddyWatchdog(app)
    # #2070: In-process LLM router backed by litellm.Router (subsystem).
    app.state.llm_router = LLMRouter(app)
    # #1480: Terminal(app_state) groups the ~25 tmux-session
    # management functions that share a Podman dependency. Reaches podman,
    # the registry, and settings through the single app_state reference.
    app.state.terminal = terminal.Terminal(app)
    # #1450: OIDC(app_state) owns the provider registry, discovery/JWKS
    # caches, and login-hook state (previously module globals). Reaches
    # config through self.settings.
    app.state.oidc = oidc.OIDC(app)
    # #1451: Features(app_state) owns the features dir (computed from
    # settings, not frozen at import), declarations, and resolved values
    # (previously module globals).
    app.state.features = features.Features(app)
    # #1484: Workspaces(app_state) owns the workspace root (computed from
    # settings.data_dir at construction, not frozen at import) + CRUD/path
    # helpers.
    app.state.workspaces = workspaces.Workspaces(app)
    # #1566: Files(app_state) owns the podman-exec file operations
    # (list/read/write/delete/rename/stream), previously free functions
    # in files.py that threaded podman through every call. The class owns
    # the podman reference, the same way Workspaces/Terminal do.
    app.state.files = files.Files(app)
    # #1452: DB(settings) owns the engine cache + data dir (computed from
    # settings, not frozen at import). Bound as the active DB for the
    # lifespan's context in the lifespan itself (#1520: no module-global
    # backstop — the model/ free functions reach it via a ContextVar).
    app.state.db = model.db.DB(app)
    # #1563 / #1572: Model(app_state) composes the per-domain data-access
    # sub-objects (tokens, login_attempts, invitations, ports here; users,
    # acl, workspaces follow). Each reaches the
    # DB via self.app.state.db — the single instance wired just above — so
    # every code path resolves the same DB (the #1551 divergence class is
    # structurally impossible for these domains). The not-yet-converted
    # domains still go through the _current_db ContextVar backstop.
    app.state.model = model.Model(app)
    # #1577: ACL(app_state) owns the FastAPI permission layer — the
    # resource-tree walk / principal resolution that the ``has_permission``
    # dependency (resolved per-request from ``request.app.state.acl``) and
    # the WebSocket connection layer delegate to. Reached through
    # ``self.app.state.model.{users,acl}``, so wired after ``app.state.model``.
    app.state.acl = acl.ACL(app)
    # #1483: EmailService(app_state) owns SMTP/sendmail transport + the
    # Jinja template env (previously module-level functions reading
    # resolve_env_value at call time).
    app.state.email = emailsvc.EmailService(app)
    # #1503: Util(app_state) owns the proxy-trust / forwarded-header logic,
    # hosting-info derivation, and customize-dir resolver (previously
    # module-level functions + import-time globals in util.py).
    app.state.util = util_mod.Util(app)
    # #1571: Lifecycle(app_state) owns the startup/shutdown/restart
    # sequence and the default-user / agent-user / ACL seeding that runs
    # at lifespan start. The lifespan and the SIGHUP restart path call
    # its methods.
    app.state.lifecycle = Lifecycle(app)

    # Middleware stack (outermost first): no-cache → LiveCORS → InFlight.
    # CORS outside the in-flight counter is deliberate: CORS preflights
    # are answered without reaching the app and must not be counted
    # (#2738 audit).
    app.add_middleware(InFlightMiddleware, counter=app.state.inflight_requests)
    app.add_middleware(LiveCORSMiddleware, fastapi_app=app)
    # Registered once here, NOT inside setup_static_files: add_middleware
    # raises RuntimeError after the app has started serving, which the
    # SIGHUP frontend remount path would hit on a live server (#2738
    # audit; previously the remount crashed the recycle).
    app.middleware("http")(no_cache_headers)

    app.include_router(root_router)
    app.include_router(router, prefix=API_PREFIX)

    register_exception_handlers(app)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
        await handle_websocket(ws, app)

    @app.websocket("/ws/consent-decider")
    async def consent_decider_endpoint(ws: WebSocket):  # pragma: no cover
        # #2308: a live consent decider registers here; its connection
        # lifecycle drives the ConsentDeciderRegistry (the interactive-mode
        # gate). Event content lands with #2244.
        await handle_consent_decider(ws, app)

    @app.websocket("/ws/egress-sidecar")
    async def egress_sidecar_endpoint(ws: WebSocket):  # pragma: no cover
        # #2311: the network sidecar sends blocked-egress events here and
        # receives verdicts (hold-and-prompt). The coordinator gate-checks
        # (hold iff a decider is registered, else static deny); the decider
        # fanout lands with #2244, the sidecar kernel hold in a follow-up.
        await handle_egress_sidecar(ws, app)

    # #2322: catch-all websocket fallback — must be registered after specific
    # ws routes but before the StaticFiles mount, which otherwise swallows ws
    # scopes and crashes with ``assert scope["type"] == "http"``.
    @app.websocket("/{path:path}")
    async def ws_fallback(ws: WebSocket, path: str):  # pragma: no cover
        await ws.accept()
        logger.warning("unhandled ws path: /%s", path)
        await ws.close(code=4044, reason=f"no websocket route at /{path}")

    # Frontend UI dir, resolved from settings (#1456, #1600). Mounted only
    # when it exists; a packaged/installed klangkd ships the UI inside the
    # wheel (klangk/frontend) so this is the common case. When the dir is
    # absent -- a misconfigured override, or a wheel built without the
    # Flutter artifact -- log a loud warning instead of silently serving an
    # API-only app (#1600).
    frontend_dir = Path(settings.frontend_dir)
    if frontend_dir.exists():
        setup_static_files(app, frontend_dir)
    else:
        logger.warning(
            "frontend_dir %s does not exist; the web UI will not be "
            "served. Point KLANGKD_FRONTEND_DIR at a built Flutter web "
            "directory, or (for a packaged install) reinstall a wheel that "
            "ships the frontend artifact (#1600).",
            frontend_dir,
        )

    return app


# --- Re-exports (back-compat for ``from klangk.main import ...``) ---
# Canonical homes are the split modules (#2738); tests and callers still
# import these names from ``klangk.main``.
from .bind_safety import (  # noqa: F401, E402
    enforce_no_auth_bind_safety,
    is_loopback_bind,
)
from .lifecycle import setup_logfire  # noqa: F401, E402


# Re-export from _common so existing callers (e.g. tests) that do
# ``from klangk.main import get_app_state_dep`` keep working.
# The canonical home is ``api._common`` (avoids main <-> api circular import).
from .api._common import get_app_dep  # noqa: F401, E402


# --- ASGI app ---
# No module-level ``app = build_app(...)`` and no ``__getattr__`` shim: the
# composition root is sealed (#1454). ``klangkd`` constructs the app
# explicitly (``build_app(settings)``) and passes the object to uvicorn. The
# E2E suites launch real ``klangkd`` (``python -m klangk.launcher``) and
# contact it over its UDS — no ``module:app`` string import anywhere
# (#1525).
