"""Klangk backend: FastAPI app composition root + the ``klangkd`` launcher.

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

The process-level launcher (previously :mod:`klangk.launcher`, merged in
#2753) lives in the second half of this module: it loads config (from a
YAML file + env vars + built-in defaults, per the precedence rules in
:mod:`klangk.settings`), binds uvicorn to the UDS at ``settings.socket``
(default ``<state_dir>/klangk.sock``, overridable via ``KLANGKD_SOCKET``),
and owns the proxy child (currently Caddy) that fronts it.

Usage::

    klangkd                          # resolves <KLANGKD_CONFIG_DIR>/klangkd.yaml;
    #                                # generates it on first run (#1645)
    klangkd --config /path/to/cfg.yaml
    klangkd --config=none            # env-vars-only (the sole opt-out)

Config-file resolution (three states, no implicit escape):

1. Bare ``klangkd`` → resolves ``$KLANGKD_CONFIG_DIR/klangkd.yaml`` (default
   ``~/.config/klangkd/klangkd.yaml``, #1649, #1646). If the file is missing it is
   **generated** as a near-empty template pointing at the docs (#1645) —
   no admin identity or password is emitted. The admin row is seeded at
   runtime: ``default_user`` defaults to ``<unixuser>@example.com`` with
   ``password_hash=None`` in ``none``/``oidc`` mode (no password needed);
   ``password``/``both`` mode requires ``KLANGKD_DEFAULT_PASSWORD`` (fail-fast
   if unset).
2. ``--config=<path>`` → that path required to exist; missing → error.
   Explicit paths are never auto-generated.
3. ``--config=none`` → run from env vars + built-in defaults (no file).

See #1392 (the design record), #1395 (config + launcher), #1396 (UDS +
proxy ownership), and #1645 (first-run generation) for the full rationale.
"""

import asyncio
import contextlib
import logging
import os
import platform
import shutil
import signal as signal_mod
import socket
import subprocess
import sys
import threading
from pathlib import Path

import typer
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from uvicorn.server import HANDLED_SIGNALS

# ``klangk.logger`` is imported first — before every other klangk
# submodule — so its module-level default logging configuration is
# active before any of them (or any ``KlangkSettings(...)`` construction,
# whose validators log) runs import-time or constructor-time logging
# (#1467, #1993). A plain statement import: it binds no ``logger`` symbol
# (klangk.logger exposes only configure/configure_defaults), keeping the
# per-module ``logger`` below free.
import klangk.logger  # noqa: F401

from . import (
    acl,
    auth,
    container,
    consent,
    emailsvc,
    files,
    first_run,
    server_schedule,
    hooks as hooks_mod,
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
from .exceptions import EX_CONFIG
from .logger import (
    configure as configure_logging,
)  # ordering: see klangk.logger above
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


# ---------------------------------------------------------------------------
# Composition root (app assembly)
# ---------------------------------------------------------------------------


async def agent_principal_error_handler(request, exc):  # noqa: ARG001
    """Reject any operation that would make the agent an ACL principal.

    Raised at the model choke points (``add_user_to_group``,
    ``add_acl_entry``, ``delete_user``, ``update_password``); translated
    to HTTP 400 here so route handlers carry no per-endpoint guard code.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def role_scope_error_handler(request, exc):  # noqa: ARG001
    """Reject cross-workspace role-group grants (#2750).

    Raised at the model choke points (``add_acl_entry`` /
    ``replace_acl_entries``) whenever an ACL write would grant a
    per-workspace role group on anything other than its own workspace's
    resource; translated to HTTP 400 here so route handlers carry no
    per-endpoint guard code.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def admin_group_protection_error_handler(request, exc):  # noqa: ARG001
    """Reject writes that would break the ``admins`` group's identity
    (#2995).

    Raised at the model choke points (``update_group`` /
    ``delete_group``) when a rename would move the ``admins`` name on or
    off a group or the group itself would be deleted — ``is_admin``
    derives from membership in a group named ``admins``; translated to
    HTTP 400 here so route handlers carry no per-endpoint guard code.
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
    application.add_exception_handler(
        model.WorkspaceRoleScopeError, role_scope_error_handler
    )
    application.add_exception_handler(
        model.AdminGroupProtectionError, admin_group_protection_error_handler
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
    # #2242/#2311: retention sweeper — prunes the bounded tables past
    # their retention window / cap (egress_consent, #2303; container_events,
    # #2924). Consent events themselves arrive over the sidecar WS
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
    app.state.sidecar_connections = sidecar_connections.SidecarConnections(app)
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
    # #2762: Hooks(app_state) owns the customize-dir lifecycle hooks
    # (currently the workspace-created hook). Loaded at lifespan start
    # and reloaded on SIGHUP (Hooks.reconfigure); the workspace-created
    # hook fires from the Workspaces service layer at creation time.
    app.state.hooks = hooks_mod.Hooks(app)
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
    async def websocket_endpoint(ws: WebSocket):
        await handle_websocket(ws, app)

    @app.websocket("/ws/consent-decider")
    async def consent_decider_endpoint(ws: WebSocket):
        # #2308: a live consent decider registers here; its connection
        # lifecycle drives the ConsentDeciderRegistry (the interactive-mode
        # gate). Event content lands with #2244.
        await handle_consent_decider(ws, app)

    @app.websocket("/ws/egress-sidecar")
    async def egress_sidecar_endpoint(ws: WebSocket):
        # #2311: the network sidecar sends blocked-egress events here and
        # receives verdicts (hold-and-prompt). The coordinator gate-checks
        # (hold iff a decider is registered, else static deny); the decider
        # fanout lands with #2244, the sidecar kernel hold in a follow-up.
        await handle_egress_sidecar(ws, app)

    # #2322: catch-all websocket fallback — must be registered after specific
    # ws routes but before the StaticFiles mount, which otherwise swallows ws
    # scopes and crashes with ``assert scope["type"] == "http"``.
    @app.websocket("/{path:path}")
    async def ws_fallback(ws: WebSocket, path: str):
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


# ---------------------------------------------------------------------------
# Process launcher (the ``klangkd`` CLI — merged from launcher.py, #2753)
# ---------------------------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Start the klangk server (config + uvicorn + proxy).",
)


def resolve_config_path(config: str | None) -> str:
    """Resolve the ``--config`` value into a path or the 'none' sentinel.

    Three cases, no implicit escape (#1392 / #1645):

    - ``None`` (bare ``klangkd``, no ``--config``) → resolve the default path
      at ``<KLANGKD_CONFIG_DIR>/klangkd.yaml`` (default
      ``~/.config/klangkd/klangkd.yaml``). **Generate on first run** if the
      file doesn't exist (#1645): writes a near-empty template pointing at
      the docs. No admin identity or password is emitted — the admin row
      is seeded at runtime (``default_user`` defaults to
      ``<unixuser>@example.com``; null hash in ``none``/``oidc`` mode,
      ``KLANGKD_DEFAULT_PASSWORD`` required in ``password``/``both``).
    - ``"none"`` → explicit env-only opt-out (no config file).
    - ``"<path>"`` → that path, required to exist. Missing → ``BadParameter``.
      Explicit paths are never auto-generated — generation only fires for
      the implicit default.

    Returns the resolved path string or ``"none"``.  Raises
    ``typer.BadParameter`` (which Typer surfaces as a clean CLI error) on a
    missing explicitly-required file.
    """
    if config is None:
        path = first_run.default_config_path()
        if not os.path.isfile(path):
            try:
                first_run.generate_default_config(path)
            except FileExistsError:
                # Race: another klangkd (e.g. a systemd restart overlap)
                # generated the file between our isfile check and the open.
                # Treat it as "the file is there now" and proceed.
                pass
        return path
    if config == "none":
        return "none"
    path = Path(config)
    if not path.is_file():
        raise typer.BadParameter(
            f"Config file not found: {config}",
            param_hint="--config",
        )
    return str(path)


def check_pid_preflight(settings: KlangkSettings) -> int | None:
    """Return the PID of a live klangkd for this instance, or ``None``.

    Mirrors :meth:`Util.check_pid_file` but runs *before* the app is
    built so the launcher can abort before touching the UDS (#1837).
    """
    # Read the instance ID the same way Util.resolve_instance_id does,
    # but read-only — don't generate one; if the file is missing there
    # is no running instance to collide with.
    instance_id_path = Path(settings.data_dir) / "instance-id"
    try:
        instance_id = instance_id_path.read_text().strip()
    except (FileNotFoundError, ValueError):
        return None
    if not instance_id:
        return None

    pid_path = Path(settings.state_dir) / f"klangk-{instance_id}.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None

    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # Stale PID file — clean it up.
        pid_path.unlink(missing_ok=True)
        return None
    except PermissionError:
        return pid

    if pid == os.getpid():
        return None
    return pid


# Per-instance marker recording the live winner PID whose duplicate-launch
# collision was already reported (#2021). The *losing* (second) process still
# logs why it is exiting — once — but a service supervisor's restart loop of
# that loser would otherwise emit one ERROR per retry into the shared log
# stream (which reads like a server fault). Keying the dedup on the live
# winner PID means: the first collision logs, subsequent retries against the
# same winner stay quiet, and a *new* winner (different PID) is reported
# fresh. The *winning* (first) process never reaches the refusal path at all
# (no pidfile on a fresh start; its own PID is excluded), so it never logs
# this — independent of whether stderr is a TTY.
REFUSAL_MARKER_SUFFIX = ".refusal"


def refusal_marker_path(settings: KlangkSettings) -> Path | None:
    """Path to the per-instance refusal marker, or ``None`` if no instance id.

    Sibling of the pidfile (``klangk-<instance>.refusal`` in the state dir).
    ``None`` (no dedup — always emit) when there is no instance id on disk.
    """
    instance_id_path = Path(settings.data_dir) / "instance-id"
    try:
        instance_id = instance_id_path.read_text().strip()
    except (FileNotFoundError, ValueError):
        return None
    if not instance_id:
        return None
    return (
        Path(settings.state_dir)
        / f"klangk-{instance_id}{REFUSAL_MARKER_SUFFIX}"
    )


def refusal_already_reported(marker: Path, winner_pid: int) -> bool:
    """True if a refusal for this live winner PID was already logged (#2021)."""
    try:
        return int(marker.read_text().strip()) == winner_pid
    except (FileNotFoundError, ValueError):
        return False


def mark_refusal_reported(marker: Path, winner_pid: int) -> None:
    """Record that a refusal for this winner PID was logged (#2021).

    Best-effort: a write failure just means the next retry logs again (one
    extra line) — never a missed refusal or a spurious "already running".
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(winner_pid))
    except OSError:
        pass


def check_port_preflight(host: str, port: int) -> bool:
    """Return True if *host*:*port* already has a listener.

    A connect-probe catches cross-deploy collisions that the PID-file
    check cannot see (different ``state_dir`` → different instance-id →
    separate PID files, so neither klangkd knows about the other).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def config_error_exit_status(app_state) -> int | None:
    """Return ``EX_CONFIG`` if the lifespan flagged a config refusal.

    The lifespan records the first deterministic ``ConfigurationError`` it
    hits during startup (password-policy-violating ``KLANGKD_DEFAULT_PASSWORD``,
    missing password in password mode, insecure JWT secret with prevention
    on, unsafe no-auth bind, …) on ``app.state.startup_config_error`` (#2666).
    The launcher consults this when uvicorn exits with a startup failure:
    uvicorn's own status (3) is indistinguishable between "config is wrong
    forever" and "transient failure, try again", so a supervisor restart-loops
    both. ``EX_CONFIG`` (78, sysexits.h) marks the permanent class — systemd
    ``RestartPreventExitStatus=78`` stops the loop.
    """
    flagged = getattr(app_state, "startup_config_error", None)
    if flagged is None:
        return None
    return EX_CONFIG


def prepend_gnubin_paths() -> None:
    """On macOS, prepend Homebrew gnubin dirs to ``PATH`` (#1947).

    macOS ships BSD ``du`` and ``tar`` whose flags are incompatible with
    klangkd's usage (``du -b``, ``tar --transform``).  Homebrew's
    ``coreutils`` and ``gnu-tar`` install GNU binaries under g-prefixed
    names (``gdu``, ``gtar``) and provide ``libexec/gnubin`` directories
    that shadow the BSD originals.

    This runs once at startup — before any subprocess is spawned — so
    every callsite (including bare ``subprocess.run(["du", ...])`` and
    the ``subprocess_env()``-based podman calls) inherits the fixed PATH.
    No-op on Linux or when ``brew`` is absent.
    """
    if platform.system() != "Darwin":
        return
    brew = shutil.which("brew")
    if not brew:
        return
    try:
        result = subprocess.run(
            [brew, "--prefix"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        prefix = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return
    if not prefix:
        return
    gnubin_dirs = [
        os.path.join(prefix, "opt", "coreutils", "libexec", "gnubin"),
        os.path.join(prefix, "opt", "gnu-tar", "libexec", "gnubin"),
    ]
    existing = [d for d in gnubin_dirs if os.path.isdir(d)]
    if existing:
        os.environ["PATH"] = (
            os.pathsep.join(existing) + os.pathsep + os.environ.get("PATH", "")
        )


@app.callback()
def main(
    ctx: typer.Context,
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Path to a YAML config file. Bare ``klangkd`` (no --config) "
            "resolves ``$KLANGKD_CONFIG_DIR/klangkd.yaml`` "
            "(default ``~/.config/klangkd/klangkd.yaml``) and generates it "
            "on first run (#1645). Use 'none' to run from env vars only "
            "(no config file)."
        ),
    ),
) -> None:
    """Start the klangk server (config + uvicorn + proxy)."""
    prepend_gnubin_paths()
    if ctx.invoked_subcommand is not None:
        return  # defer to the subcommand (e.g. ``doctor``)
    resolved = resolve_config_path(config)

    # Everything below reads through the typed config (config file > env >
    # defaults, with file:/cmd: resolution), NOT raw os.environ — so a YAML
    # value or a ``file:``/``cmd:`` prefix takes effect the same as an env
    # var (#1394/#1395). Construction runs field validators (fail-fast on
    # bogus config) before uvicorn starts.
    settings = KlangkSettings(os.environ, config_file=resolved)

    # uvicorn always binds the UDS at ``settings.socket`` (default
    # ``<state_dir>/klangk.sock``, overridable via ``KLANGKD_SOCKET`` — #1542).
    # ``KLANGKD_PORT`` (unset ⇒ headless, set ⇒ full/browser) drives the proxy's
    # rendered template + listen directives; uvicorn never listens on TCP
    # directly.
    state_dir = settings.state_dir
    os.environ["KLANGKD_STATE_DIR"] = state_dir
    uds_path = settings.socket

    # Read ws_max_size through the typed config (default 16 MiB, #1394/#1395).
    ws_max_size = settings.websocket_msg_size_max

    # Pre-flight PID check: abort *before* touching the UDS so a second
    # klangkd doesn't destroy the first instance's socket (#1837).
    # The lifespan has its own authoritative check, but that runs after
    # uvicorn binds — too late to protect the socket file.
    #
    # Only the *losing* (second) process reaches here — the *winning* (first)
    # process has no pidfile to find on a fresh start, and its own PID is
    # excluded by check_pid_preflight. The loser reports why it is exiting,
    # but de-duplicated against the live winner PID (#2021): a supervisor's
    # restart loop logs the refusal once (first collision) and then stays
    # quiet for retries against the same winner, instead of spamming one
    # ERROR per retry. A different winner PID is reported fresh.
    existing = check_pid_preflight(settings)
    if existing is not None:
        _report_pid_collision(settings, existing)
        sys.exit(1)

    # Port-probe check: catch cross-deploy collisions the PID-file
    # guard misses (#2211). Two klangkd instances from different
    # checkouts (different $DEVENV_STATE → different state_dir →
    # separate instance-id / PID files) never see each other's PID
    # file. But they share the same TCP ports (browser + egress)
    # through Caddy — probe those before starting. The proxy hasn't
    # started yet, so a live listener on our configured port means
    # another klangkd (or its proxy) already owns it.
    _check_port_collisions(settings)

    # Bind the UDS. A stale socket from a kill -9'd process makes the
    # bind fail with EADDRINUSE — unlink first (the pidfile guard in the
    # lifespan refuses a concurrent klangkd). Ensure the parent dir is
    # private (0700) so only the klangk user can open the socket — the
    # same-uid trust boundary Util.set_uds_mode relies on.
    try:
        os.unlink(uds_path)
    except FileNotFoundError:
        pass
    Path(uds_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Construct the app explicitly and pass the object to uvicorn (not a
    # ``module:app`` string import). This avoids the module-level
    # ``app = build_app()`` global — there's one ``build_app(settings)`` call,
    # one registry, wired correctly (#1464, #1454).
    asgi_app = build_app(settings)
    # Arm the UDS trust flag on the Util instance: over a UDS,
    # request.client is None, and a None peer is the trusted reverse
    # proxy (same-uid socket access). Set here, from the bind decision —
    # not via a config field (#1422 retired KLANGKD_UDS_MODE).
    asgi_app.state.util.set_uds_mode(True)

    server = make_graceful_exit_server(asgi_app)(
        uvicorn.Config(
            asgi_app,
            uds=uds_path,
            # proxy_headers=False: over a UDS request.client is None;
            # our trust helpers handle header trust via Util's uds-mode
            # flag. Letting uvicorn also rewrite client would double-resolve.
            proxy_headers=False,
            ws_max_size=ws_max_size,
            # Server stays at uvicorn's default (20/20). The TUI detects
            # a wedged / half-open connection via its own client-side
            # pings (set in ``cli/tui/ws.py``, 10s/10s) — its single
            # reachability signal (#2052) — so there's no need to
            # tighten the server globally (which would also affect the
            # web UI and `klangk monitor`).
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    )
    try:
        server.run()
    except OSError as exc:
        logger.error(
            "uvicorn failed to bind UDS at %s: %s — exiting", uds_path, exc
        )
        sys.exit(1)
    except SystemExit:
        # uvicorn exits STARTUP_FAILURE (3) for every startup failure. If the
        # failure was a deterministic config error (flagged by the lifespan
        # on app.state), translate to EX_CONFIG (78) so supervisors can stop
        # restart-looping a config that cannot fix itself (#2666).
        config_status = config_error_exit_status(asgi_app.state)
        if config_status is None:
            raise
        logger.error(
            "klangkd refused to start over a configuration error — exiting "
            "with status %d (EX_CONFIG); restarting cannot fix this, fix "
            "the config instead",
            config_status,
        )
        raise SystemExit(config_status) from None


def make_graceful_exit_server(asgi_app):
    """Build a uvicorn Server class whose SIGTERM/SIGINT exit runs the
    app's graceful-shutdown hook first (#2527).

    uvicorn has no native pre-shutdown signal hook: ``capture_signals``
    installs ``Server.handle_exit`` directly, and the ASGI lifespan
    shutdown only fires *after* every connection — including every
    WebSocket — is closed, far too late for a ``host_shutdown``
    broadcast. The sanctioned extension point is the subclass: this
    server's exit handler broadcasts + drains + refuses starts (the
    Lifecycle hook) **before** delegating to uvicorn's ``handle_exit``,
    so uvicorn's listener stop / connection drain / lifespan teardown
    sequence is untouched.

    Second signal: the hook is one-shot (``lifecycle.shutting_down``).
    A TERM/INT during the hook sets ``force_exit`` and delegates to
    uvicorn's handler — uvicorn aborts its wait loops immediately, so
    the "second Ctrl+C hard-exits" behavior is preserved (uvicorn
    alone would need a third press, because the first signal's exit is
    deferred to the hook's done-callback and should_exit is still
    unset — #2527 review).
    """

    @contextlib.contextmanager
    def capture_signals(self):
        # Mirror uvicorn's capture_signals, wrapping handle_exit with
        # the app hook. Main-thread guard as in uvicorn itself.
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = self.handle_exit

        def hooked(sig, frame):
            lifecycle = getattr(asgi_app.state, "lifecycle", None)
            ran_hook = False
            if lifecycle is not None and not lifecycle.shutting_down:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    lifecycle.shutting_down = True
                    task = loop.create_task(
                        lifecycle.graceful_shutdown(signal_num=sig)
                    )

                    # Strong reference so the GC can't reap the hook
                    # mid-drain (same hazard class as the SIGHUP restart
                    # tasks); the done-callback discards it, surfaces a
                    # hook failure, and starts uvicorn's own exit — the
                    # hook only drains; the exit handoff lives here so
                    # even a failed/raising hook still terminates the
                    # process.
                    def hook_done(_task, _orig=original):
                        lifecycle._shutdown_tasks.discard(_task)
                        if not _task.cancelled():
                            exc = _task.exception()
                            if exc is not None:
                                logger.error(
                                    "graceful-shutdown hook failed: %s",
                                    exc,
                                    exc_info=exc,
                                )
                        _orig(sig, frame)

                    lifecycle._shutdown_tasks.add(task)
                    task.add_done_callback(hook_done)
                    ran_hook = True
            if not ran_hook:
                # Second signal (hook already running / no lifecycle /
                # no loop): force uvicorn's exit NOW. Calling bare
                # handle_exit only sets should_exit — which the first
                # signal's deferred handoff hasn't set yet — so a second
                # press would merely start a concurrent graceful exit,
                # killing sockets mid-drain, and a third would be needed
                # for force_exit. Setting force_exit directly preserves
                # uvicorn's "second Ctrl+C hard-exits" semantics (#2527
                # review).
                self.force_exit = True
                original(sig, frame)

        original_handlers = {
            sig: signal_mod.signal(sig, hooked) for sig in HANDLED_SIGNALS
        }
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal_mod.signal(sig, handler)
        for captured_signal in reversed(self._captured_signals):
            signal_mod.raise_signal(captured_signal)

    return type(
        "GracefulExitServer",
        (uvicorn.Server,),
        {"capture_signals": capture_signals},
    )


@app.command()
def doctor(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show extra detail for each check."
    ),
) -> None:
    """Check for missing dependencies and common misconfigurations."""
    # allow-deferred-import (subcommand-scoped)
    from klangk.doctor import doctor_main

    raise SystemExit(doctor_main(verbose=verbose))


# --- Re-exports (back-compat for ``from klangk.main import ...``) ---
# Canonical homes are the split modules (#2738); tests and callers still
# import these names from ``klangk.main``.
from .bind_safety import (  # noqa: F401, E402
    enforce_no_auth_bind_safety,
    is_loopback_bind,
)
from .lifecycle import setup_logfire  # noqa: F401, E402


# Re-export from common so existing callers (e.g. tests) that do
# ``from klangk.main import get_app_state_dep`` keep working.
# The canonical home is ``api.common`` (avoids main <-> api circular import).
from .api.common import get_app_dep  # noqa: F401, E402


# --- ASGI app ---
# No module-level ASGI app and no ``__getattr__`` shim: the composition
# root is sealed (#1454). The module-level ``app`` above is the *Typer CLI*
# (the ``klangkd`` console-script entry point), not a pre-built FastAPI app;
# the ASGI app is constructed explicitly (``build_app(settings)``) and
# passed to uvicorn. The E2E suites launch real ``klangkd``
# (``python -m klangk.main``) and contact it over its UDS — no
# ``module:app`` string import anywhere (#1525).


def _report_pid_collision(settings, existing: int) -> None:
    """Report the losing PID-collision refusal, de-duplicated against the
    live winner PID (#2021): a supervisor's restart loop logs the refusal
    once (first collision) and then stays quiet for retries against the
    same winner, instead of spamming one ERROR per retry. A different
    winner PID is reported fresh."""
    marker = refusal_marker_path(settings)
    if marker is None or not refusal_already_reported(marker, existing):
        logger.error(
            "Another klangk instance (PID %d) is already running — "
            "refusing to start",
            existing,
        )
        if marker is not None:
            mark_refusal_reported(marker, existing)


def _check_port_collisions(settings) -> None:
    """Port-probe check: catch cross-deploy collisions the PID-file guard
    misses (#2211). Two klangkd instances from different checkouts
    (different $DEVENV_STATE → different state_dir → separate instance-id /
    PID files) never see each other's PID file. But they share the same TCP
    ports (browser + egress) through Caddy — probe those before starting.
    The proxy hasn't started yet, so a live listener on our configured port
    means another klangkd (or its proxy) already owns it."""
    for port_str, label in (
        (settings.port, "browser"),
        (settings.egress_port, "egress"),
    ):
        if port_str is None:
            continue
        port_int = int(port_str)
        listen_host = (
            settings.listen if label == "browser" else settings.egress_listen
        )
        if check_port_preflight(listen_host, port_int):
            logger.error(
                "Another process is already listening on %s:%d (%s port) "
                "— refusing to start. Is another klangkd running?",
                listen_host,
                port_int,
                label,
            )
            sys.exit(1)


if __name__ == "__main__":  # pragma: no cover — module-exec arm
    # (python -m klangk.main) never runs under in-process tests.
    app()
