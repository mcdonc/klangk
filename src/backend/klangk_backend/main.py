"""Klangk backend: FastAPI app with HTTP + WebSocket endpoints."""

import asyncio
import ipaddress
import logging
import os
import secrets
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import bcrypt
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    agent,
    auth,
    container,
    emailsvc,
    model,
    nginx as nginx_mod,
    oidc,
    plugins,
    podman,
    ssl_trust,
    terminal,
    util as util_mod,
    workspaces,
    wshandler,
)
from .settings import KlangkSettings
from .api import root_router, router
from .util import API_PREFIX
from .model import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
from .model import AGENT_USER_ID
from .wshandler import handle_websocket

_LIGHT_BLUE = "\033[94m"
_GREEN = "\033[32m"
_RESET = "\033[0m"

logging.basicConfig(
    level=logging.INFO,
    format=f"{_LIGHT_BLUE}%(asctime)s %(levelname)s:%(name)s:%(message)s{_RESET}",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def seed_default_acls(admin_group_id: str) -> None:
    """Seed default ACL entries if none exist yet."""
    existing = await model.get_acl_tree_summary()
    if existing:
        return
    # /: Authenticated users can view, deny everyone else
    await model.add_acl_entry(
        "/",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await model.add_acl_entry(
        "/",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    # /workspaces: Authenticated users can create
    await model.add_acl_entry(
        "/workspaces",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    # /groups: Authenticated users can create groups
    await model.add_acl_entry(
        "/groups",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    # /admin: admin group gets full access, deny everyone else
    await model.add_acl_entry(
        "/admin",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_GROUP,
        group_id=admin_group_id,
    )
    await model.add_acl_entry(
        "/admin",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    logger.info("Seeded default ACL entries")


async def ensure_admin_group() -> str:
    """Ensure the 'admin' group exists. Returns the group ID."""
    group = await model.get_group_by_name("admin")
    if group is None:
        group = await model.create_group("admin", description="Administrators")
        logger.info("Created admin group: %s", group["id"])
    return group["id"]


# Addresses that are safe for no-auth single-user (``none``) mode: only the
# loopback interface is reachable from the host browser and not from other
# machines or from workspace containers (which appear via pasta NAT as the
# host's non-loopback IP). ``0.0.0.0`` / ``::`` bind every interface and are
# NOT loopback. The full IPv4 loopback range (127.0.0.0/8) and IPv6 ``::1``
# are admitted via :func:`ipaddress.is_loopback`; the bare hostname
# ``localhost`` is admitted as a special case (it resolves to loopback but is
# not itself an IP literal). A UNIX socket path is also safe — ``klangkd``
# creates the parent directory with mode 0700, so only the same uid can
# connect (the same trust boundary as loopback). See #1374.
def _is_loopback_bind(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def enforce_no_auth_bind_safety(app_state) -> None:
    """Refuse to start in ``none`` auth mode unless the browser bind is loopback.

    ``KLANGK_AUTH_MODES=none`` freely issues a token for the seeded default
    user (``POST /api/v1/auth/local``); anyone who can reach that endpoint is
    effectively logged in as admin. In full/browser mode (`KLANGK_PORT` set),
    the loopback browser bind (`KLANGK_LISTEN`) is the identity boundary — it
    keeps the endpoint reachable from the operator's own browser but not from
    the network or from workspace containers. Override the gate explicitly
    with ``KLANGK_ALLOW_INSECURE_NO_AUTH=1`` when you knowingly expose a
    no-auth server (e.g. a throwaway VM on an isolated network). #1374.

    In headless mode (`KLANGK_PORT` unset) there is no browser listener at
    all — the backend serves only the UDS (same-uid trust boundary), and
    ``/auth/local`` is never exposed over TCP — so the gate is a no-op (#1542).
    """
    if app_state.oidc.auth_modes() != "none":
        return
    # Headless: no browser listener rendered → /auth/local not exposed on TCP.
    if app_state.settings.port is None:
        return
    host = app_state.settings.listen
    if _is_loopback_bind(host):
        return
    if app_state.settings.allow_insecure_no_auth.strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.warning(
            "KLANGK_AUTH_MODES=none with non-loopback bind %r — allowed "
            "because KLANGK_ALLOW_INSECURE_NO_AUTH=1. Anyone who can reach "
            "this address is effectively logged in as the default admin user.",
            host,
        )
        return
    raise SystemExit(
        "Refusing to start: KLANGK_AUTH_MODES=none but KLANGK_LISTEN=%r "
        "is not a loopback address. no-auth mode freely issues an admin "
        "token, so it must bind loopback (127.0.0.0/8, ::1, or localhost). "
        "Set KLANGK_LISTEN=127.0.0.1, or set KLANGK_ALLOW_INSECURE_NO_AUTH=1 "
        "to override if you understand the risk. See #1374." % host
    )


async def seed_default_user(settings) -> None:
    """Create default user if it doesn't exist.

    If KLANGK_DEFAULT_PASSWORD is set, use it. Otherwise generate a random
    password and print it to the console (only on first creation).
    """
    admin_group_id = await ensure_admin_group()
    await seed_default_acls(admin_group_id)

    email = settings.default_user
    password = settings.default_password
    existing = await model.get_user_by_email(email)
    if existing is None:
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(16)
        password_hash = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()
        user = await model.create_user(email, password_hash, verified=True)
        await model.add_user_to_group(user["id"], admin_group_id)
        if generated:
            logger.info(
                "Created default admin user '%s' (password printed to stderr)",
                email,
            )
            # Print password to stderr only — keep it out of structured
            # logs where it could be shipped to a log aggregator.
            print(
                f"{_GREEN}Default admin password for"
                f" '{email}': {password}{_RESET}",
                file=sys.stderr,
            )
        else:
            logger.info("Created default user '%s' in admin group", email)
    else:
        # Ensure existing default user is in admin group
        await model.add_user_to_group(existing["id"], admin_group_id)


async def seed_agent_user(settings) -> None:
    """Ensure the chat agent user exists in the DB.

    Reads email/handle from env vars (with defaults) and upserts the
    agent row.  This is the ONLY place the env vars are consulted.

    Refuses to seed the agent with a handle already owned by another user.
    A colliding agent handle is destructive: ``ensure_home_symlink`` would
    later migrate that user's home files into the agent's tree via its
    workspace-import adoption branch.  The ``users.handle`` UNIQUE
    constraint is the structural backstop, but we fail loudly here with an
    actionable message instead of letting a bare ``IntegrityError`` abort
    startup mid-sequence.  See #1137.
    """
    email = settings.agent_email
    handle = settings.agent_handle
    async with model.transaction() as db:
        # Pre-check: refuse a handle already claimed by a non-agent user.
        # Runs in the same transaction as the upsert so there is no
        # check-then-act window.
        cursor = await db.execute(
            "SELECT id FROM users WHERE handle = ? AND id != ?",
            (handle, AGENT_USER_ID),
        )
        if await cursor.fetchone() is not None:
            raise RuntimeError(
                f"Cannot seed chat agent: handle {handle!r} is already used"
                " by another user. Set KLANGK_AGENT_HANDLE to a"
                " unique value."
            )
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified,"
            " provider, handle)"
            " VALUES (?, ?, NULL, 1, 'system', ?)"
            " ON CONFLICT(id) DO UPDATE SET email = ?, handle = ?",
            (AGENT_USER_ID, email, handle, email, handle),
        )
    model.clear_agent_cache()
    logger.info("Seeded agent user '%s' (%s)", handle, email)


# --- PID file helpers ---


def runtime_dir() -> Path:
    """Per-user runtime dir for the PID file.

    Prefer XDG_RUNTIME_DIR (set on most Linux desktops), then the Linux
    /run/user/<uid> location, and finally ~/.klangk/run/ as a portable
    fallback (macOS has no /run and tempfile.gettempdir() may return
    per-process dirs under App Sandbox).
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg)
    linux_run = Path(f"/run/user/{os.getuid()}")
    if linux_run.is_dir():
        return linux_run
    fallback = Path.home() / ".klangk" / "run"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def pid_file_path(instance_id: str) -> Path:
    """Return the PID file path for ``instance_id``."""
    return runtime_dir() / f"klangk-{instance_id}.pid"


def check_pid_file(instance_id: str) -> int | None:
    """Check if another instance is running.

    Returns the PID of the running process, or None if no live process
    holds the PID file.  Removes stale PID files automatically.
    """
    path = pid_file_path(instance_id)
    try:
        pid = int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        # Process is dead or PID is invalid — stale PID file.
        path.unlink(missing_ok=True)
        return None
    except PermissionError:
        # Process exists but we can't signal it (different user).
        return pid
    # Don't treat our own PID as a conflict (e.g., after a crash that
    # left the PID file behind and the OS recycled the PID).
    if pid == os.getpid():
        return None
    return pid


def write_pid_file(instance_id: str) -> None:
    """Write the current PID to the instance PID file."""
    path = pid_file_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def remove_pid_file(instance_id: str) -> None:
    """Remove the PID file (best-effort)."""
    try:
        path = pid_file_path(instance_id)
        # Only remove if it contains our PID (another instance may
        # have overwritten it after we were signalled to stop).
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (FileNotFoundError, ValueError, OSError):
        pass


async def startup(app_state) -> None:
    """Container-side startup (self-healing on re-run).

    Warms podman, reaps leftover containers from a previous run, launches
    the idle and health background loops, and auto-starts workspaces.
    Every
    step is idempotent -- ``init_db`` uses ``CREATE TABLE IF NOT
    EXISTS``, the loop starters are gated on ``task is None``, and
    ``auto_start`` re-creates stopped containers -- so re-running this
    after ``runtime_shutdown`` is exactly the SIGHUP restart path.
    """
    registry = app_state.container_registry
    await registry.prewarm_podman()
    await registry.reap_instance_containers()
    registry.start_cleanup_loop()
    registry.start_health_loop()
    n = await app_state.workspaces.auto_start_workspaces()
    if n:  # pragma: no cover
        logger.info("Auto-started %d workspace(s)", n)


async def runtime_shutdown(app_state) -> None:
    """Stop the runtime, keeping the HTTP listener and DB alive.

    Drops every WebSocket client (code 1012 = "reconnect"), tears down
    agent subprocesses and in-flight agent runs, then stops all
    containers and cancels the idle/health loops.  Used by both the
    normal process-shutdown path and the SIGHUP restart path -- the
    difference is only whether ``startup()`` runs again afterwards.
    """
    await wshandler.disconnect_all_websockets(app_state.sockets)
    await app_state.agents.stop_all_sessions()
    wshandler.clear_agent_mention_state()
    await app_state.container_registry.shutdown()


async def process_shutdown(app_state) -> None:
    """Full process teardown (run once, at the very end)."""
    # instance_id() resolves from the file if startup didn't get there; if
    # there's genuinely no PID file (startup crashed early) remove_pid_file
    # no-ops on the missing file.
    remove_pid_file(app_state.util.instance_id())
    await app_state.db.dispose_engine()


# ---------------------------------------------------------------------------
# nginx child-process ownership (#1396, #1463)
# ---------------------------------------------------------------------------
# When the server binds a UDS (only klangkd does this), Python owns the nginx
# child: it renders nginx.conf, spawns nginx pointing at the UDS, and supervises
# it with a small async watchdog (spawn + await proc.wait() + respawn-with-
# backoff + clean SIGTERM to the process group on shutdown). No external
# supervisor library — bespoke, matching uvicorn's own precedent. devenv /
# supervisord remain only the outer restart layer for uvicorn (klangkd).

# Serializes concurrent SIGHUP-triggered restarts so a second signal
# arriving mid-restart queues behind the first instead of racing.
_restart_lock: asyncio.Lock | None = None


async def restart_runtime(app_state, nginx_watchdog) -> None:
    """Graceful runtime restart: stop containers, keep the listener.

    Triggered by SIGHUP.  Closes all WebSocket clients (code 1012),
    stops containers and background loops, then re-runs container-side
    startup (prewarm, adopt, loops, auto-start).  The HTTP listener
    and DB engine stay up throughout -- clients reconnect
    automatically, and in-flight HTTP requests are never dropped.
    """
    global _restart_lock
    if _restart_lock is None:
        _restart_lock = asyncio.Lock()
    async with _restart_lock:
        logger.info("SIGHUP: restarting runtime (keeping HTTP listener)")
        await runtime_shutdown(app_state)
        await startup(app_state)
        logger.info("SIGHUP: runtime restarted")


def on_sighup(app_state, nginx_watchdog) -> None:
    """Schedule a runtime restart on the running event loop.

    Signal callbacks can't be async, so this just creates a task.  The
    restart itself is serialized by ``_restart_lock``.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - no loop during shutdown
        return
    loop.create_task(restart_runtime(app_state, nginx_watchdog))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # #1520: bind app.state.db as the active DB for this lifespan's context
    # before any DB access. Request/SIGHUP tasks inherit this context, so the
    # model/ free functions (transaction/fetchone/get_db) reach the right DB
    # without a module global. Reset on teardown so a follow-up bind (tests)
    # starts clean.
    _db_token = model.db.set_current_db(app.state.db)
    await model.init_db()
    instance_id = app.state.util.resolve_instance_id()

    existing_pid = check_pid_file(instance_id)
    if existing_pid is not None:
        logger.error(
            "Another klangk instance (PID %d) is already running "
            "for instance %s — refusing to start",
            existing_pid,
            instance_id,
        )
        raise SystemExit(1)
    write_pid_file(instance_id)

    # Make the backend process itself trust deployer-supplied CAs (#1181)
    # before any outbound TLS happens (OIDC discovery, SMTP relay, LLM-proxy
    # upstream). No-op when KLANGK_SSL_CERT_DIR is unset or empty of certs.
    ssl_trust.apply_backend_ssl_trust(app.state.settings)

    # Configure Logfire *after* SSL trust is applied. logfire.configure()
    # probes the Logfire API at configuration time, so it must run once the
    # SSL_* env vars (pointing at the merged CA bundle) are set, or it
    # emits an unreachable-API warning against a private-CA endpoint (#1406).
    # Was previously called at module scope, which runs before this lifespan
    # and therefore before trust is applied.
    setup_logfire(app)

    app.state.auth.require_secure_jwt_secret()
    app.state.plugins.load()
    app.state.oidc.init_providers()
    enforce_no_auth_bind_safety(app.state)
    app.state.oidc.load_login_hook()
    await seed_default_user(app.state.settings)
    await seed_agent_user(app.state.settings)
    registry = app.state.container_registry

    async def _on_workspace_killed(ws_id):
        await wshandler.reset_workspace_state(app.state.sockets, ws_id)

    registry.set_on_workspace_killed(_on_workspace_killed)
    registry.set_on_container_status_changed(
        app.state.sockets.notify_container_status
    )
    await startup(app.state)
    # Start nginx (only when bound to a UDS — klangkd; no-op for TCP tests).
    # Rendered + owned by Python (#1396); replaces scripts/nginx.sh.
    await app.state.nginx_watchdog.start()
    logger.info("Klangk backend started")

    # uvicorn only handles SIGINT/SIGTERM, so SIGHUP is ours to claim:
    # the default disposition would kill the process, but we use it for
    # an in-place runtime restart that keeps the HTTP listener up
    # (#1212).
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGHUP,
        on_sighup,
        app.state,
        app.state.nginx_watchdog,
    )
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGHUP)
        await app.state.nginx_watchdog.stop()
        await runtime_shutdown(app.state)
        await process_shutdown(app.state)
        model.db.reset_current_db(_db_token)
        logger.info("Klangk backend stopped")


def setup_logfire(app: FastAPI) -> bool:
    """Enable Logfire instrumentation if LOGFIRE_TOKEN is set."""
    if not os.environ.get("LOGFIRE_TOKEN"):
        return False
    import logfire  # noqa: allow-deferred-import

    base_url = os.environ.get("LOGFIRE_BASE_URL")
    environment = os.environ.get("LOGFIRE_ENVIRONMENT")
    kwargs: dict = {}
    if environment:
        kwargs["environment"] = environment
    if base_url:
        # The top-level `base_url` argument is deprecated; pass it via
        # `advanced=logfire.AdvancedOptions(base_url=...)` instead (#1410).
        kwargs["advanced"] = logfire.AdvancedOptions(base_url=base_url)
    logfire.configure(**kwargs)
    logfire.instrument_fastapi(app)
    logger.info("Logfire instrumentation enabled")
    return True


async def _agent_principal_error_handler(request, exc):  # noqa: ARG001
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
        model.AgentPrincipalError, _agent_principal_error_handler
    )


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files and add no-cache middleware.

    Optionally mounts a branding directory at ``/branding`` so a custom
    logo / assets can be served without a Flutter rebuild.  Prefers
    ``<KLANGK_CUSTOMIZE_DIR>/branding`` when it exists; falls back to
    ``<KLANGK_DATA_DIR>/branding`` if that exists.  If neither directory
    exists, the ``/branding`` mount is skipped entirely.  Mounted before
    the catch-all ``/`` frontend mount so it takes priority, and without
    ``html=True`` (no directory listing). See #1152, #1360.
    """
    static_app = StaticFiles(directory=str(frontend_dir), html=True)

    candidate = Path(app.state.util.customize_dir()) / "branding"
    if candidate.is_dir():
        branding_dir = candidate
    else:
        fallback = Path(app.state.settings.data_dir) / "branding"
        branding_dir = fallback if fallback.is_dir() else None
    if branding_dir is not None:
        logger.info("Branding served from %s", branding_dir)
        app.mount(
            "/branding",
            StaticFiles(directory=str(branding_dir)),
            name="branding",
        )

    @app.middleware("http")
    async def add_no_cache_headers(request, call_next):
        response = await call_next(request)
        if (
            request.url.path.endswith((".html", ".js"))
            or request.url.path == "/"
        ):
            response.headers["Cache-Control"] = (
                "no-cache, no-store, must-revalidate"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    app.mount("/", static_app, name="frontend")


def build_app(settings: KlangkSettings) -> FastAPI:
    """Single composition root (#1426).

    Constructs the FastAPI app, wires middleware, routers, exception
    handlers, the WebSocket endpoint, and static files. The ASGI app is the
    *only* global; everything else is reached per-request via
    :func:`get_app_state_dep` (or ``app.state`` for non-request code).
    """
    app = FastAPI(title="Klangk", lifespan=lifespan)
    app.state.settings = settings
    # #1501: Auth(app_state) owns every auth config value and JWT
    # operation (previously module-level globals + import-time
    # resolve_env_value reads in auth.py). Reads self.settings at
    # construction/call time.
    app.state.auth = auth.Auth(app.state)
    # #1468: Podman(settings) owns the resolved binary path + the ~20 CLI
    # wrappers. Constructed before the registry/terminal so they reach it
    # via self.app_state.podman (#1426).
    app.state.podman = podman.Podman(settings)
    # Slice 2c (#1475): the WebSocketState is an owned instance wired onto
    # app.state.sockets. Constructed before the registry so it reaches it
    # via self.app_state.sockets — no module-level singleton.
    app.state.sockets = wshandler.WebSocketState(app.state)
    # Slice 2 (#1449): the container registry is an owned instance, not a
    # module global. The lifespan reads app.state.container_registry.
    app.state.container_registry = container.ContainerRegistry(app.state)
    # Slice 2b (#1463): nginx watchdog is an owned instance with start/stop
    # lifecycle methods called by the lifespan.
    app.state.nginx_watchdog = nginx_mod.NginxWatchdog(app.state)
    # #1480: Terminal(app_state) groups the ~25 tmux-session
    # management functions that share a Podman dependency. Reaches podman,
    # the registry, and settings through the single app_state reference.
    app.state.terminal = terminal.Terminal(app.state)
    # #1450: OIDC(app_state) owns the provider registry, discovery/JWKS
    # caches, and login-hook state (previously module globals). Reaches
    # config through self.settings.
    app.state.oidc = oidc.OIDC(app.state)
    # #1451: Plugins(app_state) owns the plugins dir (computed from
    # settings, not frozen at import), declarations, and resolved values
    # (previously module globals).
    app.state.plugins = plugins.Plugins(app.state)
    # #1484: Workspaces(app_state) owns the workspace root (computed from
    # settings.data_dir at construction, not frozen at import) + CRUD/path
    # helpers.
    app.state.workspaces = workspaces.Workspaces(app.state)
    # #1452: DB(settings) owns the engine cache + data dir (computed from
    # settings, not frozen at import). Bound as the active DB for the
    # lifespan's context in the lifespan itself (#1520: no module-global
    # backstop — the model/ free functions reach it via a ContextVar).
    app.state.db = model.db.DB(settings)
    app.state.agents = agent.Agents(app.state)
    # #1483: EmailService(app_state) owns SMTP/sendmail transport + the
    # Jinja template env (previously module-level functions reading
    # resolve_env_value at call time).
    app.state.email = emailsvc.EmailService(app.state)
    # #1503: Util(app_state) owns the proxy-trust / forwarded-header logic,
    # hosting-info derivation, and customize-dir resolver (previously
    # module-level functions + import-time globals in util.py).
    app.state.util = util_mod.Util(app.state)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app.state.util.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(root_router)
    app.include_router(router, prefix=API_PREFIX)

    register_exception_handlers(app)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
        await handle_websocket(ws, app.state)

    # Frontend UI dir, resolved from settings (#1456). Mounted only when it
    # exists (absent in installed-package deployments -> UI not served).
    frontend_dir = Path(settings.frontend_dir)
    if frontend_dir.exists():  # pragma: no cover
        setup_static_files(app, frontend_dir)

    return app


# Re-export from _common so existing callers (e.g. tests) that do
# ``from klangk_backend.main import get_app_state_dep`` keep working.
# The canonical home is ``api._common`` (avoids main <-> api circular import).
from .api._common import get_app_state_dep  # noqa: F401, E402


# --- ASGI app ---
# No module-level ``app = build_app(...)`` and no ``__getattr__`` shim: the
# composition root is sealed (#1454). ``klangkd`` constructs the app
# explicitly (``build_app(settings)``) and passes the object to uvicorn. The
# E2E suites launch ``e2e-tests/runtestserver.py``, which builds the app and
# passes the object to uvicorn — no ``module:app`` string import anywhere.
