"""Klangk backend: FastAPI app with HTTP + WebSocket endpoints."""

import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import bcrypt
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, container, model, oidc, plugins, workspaces, wshandler
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
from .util import resolve_env_value
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


async def seed_default_user() -> None:
    """Create default user if it doesn't exist.

    If KLANGK_DEFAULT_PASSWORD is set, use it. Otherwise generate a random
    password and print it to the console (only on first creation).
    """
    admin_group_id = await ensure_admin_group()
    await seed_default_acls(admin_group_id)

    email = resolve_env_value("KLANGK_DEFAULT_USER", "admin@example.com")
    password = resolve_env_value("KLANGK_DEFAULT_PASSWORD")
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


async def seed_agent_user() -> None:
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
    email = resolve_env_value("KLANGK_CHAT_AGENT_EMAIL", "MrBoops@example.com")
    handle = resolve_env_value("KLANGK_CHAT_AGENT_HANDLE", "MrBoops")
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
                " by another user. Set KLANGK_CHAT_AGENT_HANDLE to a"
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


def _runtime_dir() -> Path:
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


def _pid_file_path() -> Path:
    """Return the PID file path for this instance."""
    return _runtime_dir() / f"klangk-{container.INSTANCE_ID}.pid"


def check_pid_file() -> int | None:
    """Check if another instance is running.

    Returns the PID of the running process, or None if no live process
    holds the PID file.  Removes stale PID files automatically.
    """
    path = _pid_file_path()
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


def write_pid_file() -> None:
    """Write the current PID to the instance PID file."""
    path = _pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def remove_pid_file() -> None:
    """Remove the PID file (best-effort)."""
    try:
        path = _pid_file_path()
        # Only remove if it contains our PID (another instance may
        # have overwritten it after we were signalled to stop).
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (FileNotFoundError, ValueError, OSError):
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    existing_pid = check_pid_file()
    if existing_pid is not None:
        logger.error(
            "Another klangk instance (PID %d) is already running "
            "for instance %s — refusing to start",
            existing_pid,
            container.INSTANCE_ID,
        )
        raise SystemExit(1)
    write_pid_file()

    auth.require_secure_jwt_secret()
    plugins.load()
    await model.init_db()
    oidc.init_providers()
    oidc.load_login_hook()
    await seed_default_user()
    await seed_agent_user()
    container.registry.set_on_workspace_killed(wshandler.reset_workspace_state)
    container.registry.set_on_container_status_changed(
        wshandler.state.notify_container_status
    )
    await container.registry.prewarm_podman()
    await container.registry.adopt_orphaned_containers()
    container.registry.start_cleanup_loop()
    container.registry.start_health_loop()
    n = await workspaces.auto_start_workspaces()
    if n:  # pragma: no cover
        logger.info("Auto-started %d workspace(s)", n)
    logger.info("Klangk backend started")
    yield
    await container.registry.shutdown()
    remove_pid_file()
    await model.dispose_engine()
    logger.info("Klangk backend stopped")


app = FastAPI(title="Klangk", lifespan=lifespan)


def setup_logfire(app: FastAPI) -> bool:
    """Enable Logfire instrumentation if LOGFIRE_TOKEN is set."""
    if not resolve_env_value("LOGFIRE_TOKEN"):
        return False
    import logfire  # noqa: allow-deferred-import

    base_url = resolve_env_value("LOGFIRE_BASE_URL")
    environment = resolve_env_value("LOGFIRE_ENVIRONMENT")
    kwargs: dict = {}
    if base_url:
        kwargs["base_url"] = base_url
    if environment:
        kwargs["environment"] = environment
    logfire.configure(**kwargs)
    logfire.instrument_fastapi(app)
    logger.info("Logfire instrumentation enabled")
    return True


setup_logfire(app)


def _cors_origins() -> list[str]:
    """Build the CORS allowed-origins list.

    Priority: KLANGK_CORS_ORIGINS (comma-separated) > derived from
    hosting env vars > localhost with nginx port.
    """
    explicit = resolve_env_value("KLANGK_CORS_ORIGINS")
    if explicit:
        return [o.strip() for o in explicit.split(",") if o.strip()]
    hostname = resolve_env_value("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_value("KLANGK_HOSTING_PROTO", "http")
    if hostname:
        return [f"{proto}://{hostname}"]
    nginx_port = resolve_env_value("KLANGK_NGINX_PORT", "8995")
    return [f"http://localhost:{nginx_port}"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(root_router)
app.include_router(router, prefix=API_PREFIX)


async def _agent_principal_error_handler(request, exc):  # noqa: ARG001
    """Reject any operation that would make the agent an ACL principal.

    Raised at the model choke points (``add_user_to_group``,
    ``add_acl_entry``, ``delete_user``, ``update_password``); translated
    to HTTP 400 here so route handlers carry no per-endpoint guard code.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def register_exception_handlers(application: FastAPI) -> None:
    """Register global exception handlers on a FastAPI application.

    Called for the production app below and by the test app fixture so
    both surface the same handler wiring without duplicating the handler.
    """
    application.add_exception_handler(
        model.AgentPrincipalError, _agent_principal_error_handler
    )


register_exception_handlers(app)


# --- WebSocket ---


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
    await handle_websocket(ws)


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files and add no-cache middleware.

    Also mounts a deployer-writable branding directory at ``/branding``
    (under ``KLANGK_DATA_DIR/branding``) so a custom logo / assets can be
    served without a Flutter rebuild. Created if missing so a deployer can
    just drop files in. Mounted before the catch-all ``/`` frontend mount
    so it takes priority, and without ``html=True`` (no directory listing).
    See #1152.
    """
    static_app = StaticFiles(directory=str(frontend_dir), html=True)

    branding_dir = (
        Path(
            resolve_env_value(
                "KLANGK_DATA_DIR", str(Path.home() / ".klangk" / "data")
            )
        )
        / "branding"
    )
    branding_dir.mkdir(parents=True, exist_ok=True)
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


_frontend_dir = (
    Path(__file__).parent.parent.parent / "frontend" / "build" / "web"
)
if _frontend_dir.exists():  # pragma: no cover
    setup_static_files(app, _frontend_dir)
