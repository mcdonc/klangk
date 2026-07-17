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
    acl,
    agent,
    auth,
    container,
    emailsvc,
    files,
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


# Settings that a SIGHUP reload re-resolves and validates but CANNOT apply
# without a full process restart: the HTTP listener is bound for the life of
# the process, and the DB engine + on-disk state dir are already open/written.
# A change here is logged at warning level (so the operator knows it didn't
# take effect) rather than silently ignored (#1587).
_NON_RELOADABLE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("port", "the HTTP listener is already bound"),
    ("listen", "the HTTP listener is already bound"),
    ("data_dir", "the DB engine is already open"),
    ("state_dir", "instance state is already on disk"),
)


class Lifecycle:
    """App-level bringup/shutdown and DB seeding (#1571).

    Owns the startup/shutdown/restart sequence plus the default-user,
    agent-user, and ACL seeding that runs at lifespan start. Constructed
    once in :func:`build_app` and stored on ``app.state.lifecycle``, the
    same ``X(app_state)`` pattern every other owned subsystem uses
    (``Auth``, ``Workspaces``, ``ContainerRegistry``, ...). The lifespan
    and the SIGHUP restart path call its methods rather than module-level
    free functions; concurrent SIGHUP signals serialize on a per-instance
    lock so a second signal arriving mid-restart queues behind the first
    instead of racing.

    Pure helpers with no ``app_state`` dependency
    (:func:`_is_loopback_bind`, :func:`enforce_no_auth_bind_safety`,
    :func:`setup_logfire`, :func:`register_exception_handlers`) stay
    module-level.
    """

    def __init__(self, app):
        self.app = app
        # Serializes concurrent SIGHUP-triggered restarts so a second
        # signal arriving mid-restart queues behind the first instead of
        # racing. Lazily created on first restart so the lock binds to the
        # running event loop (the constructor runs in build_app, outside a
        # loop).
        self._restart_lock: asyncio.Lock | None = None

    def reconfigure(self, app) -> None:
        self.app = app
        self._pending_agent_reseed = True

    async def apply_pending_reseed(self) -> None:
        """Re-seed the agent user if flagged by reconfigure (#1587)."""
        if not getattr(self, "_pending_agent_reseed", False):
            return
        self._pending_agent_reseed = False
        await self.seed_agent_user()

    async def seed_default_acls(self, admin_group_id: str) -> None:
        """Seed default ACL entries if none exist yet."""
        existing = await self.app.state.model.acl.get_acl_tree_summary()
        if existing:
            return
        # /: Authenticated users can view, deny everyone else
        await self.app.state.model.acl.add_acl_entry(
            "/",
            0,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        await self.app.state.model.acl.add_acl_entry(
            "/",
            1,
            ACTION_DENY,
            "*",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_EVERYONE,
        )
        # /workspaces: Authenticated users can create
        await self.app.state.model.acl.add_acl_entry(
            "/workspaces",
            0,
            ACTION_ALLOW,
            "create",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        # /groups: Authenticated users can create groups
        await self.app.state.model.acl.add_acl_entry(
            "/groups",
            0,
            ACTION_ALLOW,
            "create",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        # /admin: admin group gets full access, deny everyone else
        await self.app.state.model.acl.add_acl_entry(
            "/admin",
            0,
            ACTION_ALLOW,
            "*",
            PRINCIPAL_GROUP,
            group_id=admin_group_id,
        )
        await self.app.state.model.acl.add_acl_entry(
            "/admin",
            1,
            ACTION_DENY,
            "*",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_EVERYONE,
        )
        logger.info("Seeded default ACL entries")

    async def ensure_admin_group(self) -> str:
        """Ensure the 'admin' group exists. Returns the group ID."""
        group = await self.app.state.model.users.get_group_by_name("admin")
        if group is None:
            group = await self.app.state.model.users.create_group(
                "admin", description="Administrators"
            )
            logger.info("Created admin group: %s", group["id"])
        return group["id"]

    async def seed_default_user(self) -> None:
        """Create default user if it doesn't exist.

        If KLANGK_DEFAULT_PASSWORD is set, use it. Otherwise generate a
        random password and print it to the console (only on first
        creation).
        """
        settings = self.app.state.settings
        admin_group_id = await self.ensure_admin_group()
        await self.seed_default_acls(admin_group_id)

        email = settings.default_user
        password = settings.default_password
        existing = await self.app.state.model.users.get_user_by_email(email)
        if existing is None:
            generated = password is None
            if generated:
                password = secrets.token_urlsafe(16)
            password_hash = bcrypt.hashpw(
                password.encode(), bcrypt.gensalt()
            ).decode()
            user = await self.app.state.model.users.create_user(
                email, password_hash, verified=True
            )
            await self.app.state.model.users.add_user_to_group(
                user["id"], admin_group_id
            )
            if generated:
                logger.info(
                    "Created default admin user '%s'"
                    " (password printed to stderr)",
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
            await self.app.state.model.users.add_user_to_group(
                existing["id"], admin_group_id
            )

    async def seed_agent_user(self) -> None:
        """Ensure the chat agent user exists in the DB.

        Reads email/handle from env vars (with defaults) and upserts the
        agent row.  This is the ONLY place the env vars are consulted.

        Refuses to seed the agent with a handle already owned by another
        user.  A colliding agent handle is destructive:
        ``ensure_home_symlink`` would later migrate that user's home files
        into the agent's tree via its workspace-import adoption branch.
        The ``users.handle`` UNIQUE constraint is the structural backstop,
        but we fail loudly here with an actionable message instead of
        letting a bare ``IntegrityError`` abort startup mid-sequence.
        See #1137.
        """
        settings = self.app.state.settings
        email = settings.agent_email
        handle = settings.agent_handle
        async with self.app.state.db.transaction() as db:
            # Pre-check: refuse a handle already claimed by a non-agent user.
            # Runs in the same transaction as the upsert so there is no
            # check-then-act window.
            cursor = await db.execute(
                "SELECT id FROM users WHERE handle = ? AND id != ?",
                (handle, AGENT_USER_ID),
            )
            if await cursor.fetchone() is not None:
                raise RuntimeError(
                    f"Cannot seed chat agent: handle {handle!r} is already"
                    " used by another user. Set KLANGK_AGENT_HANDLE to a"
                    " unique value."
                )
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified,"
                " provider, handle)"
                " VALUES (?, ?, NULL, 1, 'system', ?)"
                " ON CONFLICT(id) DO UPDATE SET email = ?, handle = ?",
                (AGENT_USER_ID, email, handle, email, handle),
            )
        self.app.state.model.users.clear_agent_cache()
        logger.info("Seeded agent user '%s' (%s)", handle, email)

    async def startup(self) -> None:
        """Container-side startup (self-healing on re-run).

        Warms podman, reaps leftover containers from a previous run,
        launches the idle and health background loops, and auto-starts
        workspaces. Every step is idempotent -- ``init_db`` uses
        ``CREATE TABLE IF NOT EXISTS``, the loop starters are gated on
        ``task is None``, and ``auto_start`` re-creates stopped containers
        -- so re-running this after ``runtime_shutdown`` is exactly the
        SIGHUP restart path.
        """
        state = self.app.state
        registry = state.container_registry
        await registry.prewarm_podman()
        await registry.reap_instance_containers()
        registry.start_cleanup_loop()
        registry.start_health_loop()
        n = await state.workspaces.auto_start_workspaces()
        if n:  # pragma: no cover
            logger.info("Auto-started %d workspace(s)", n)

    async def runtime_shutdown(self) -> None:
        """Stop the runtime, keeping the HTTP listener and DB alive.

        Drops every WebSocket client (code 1012 = "reconnect"), tears down
        agent subprocesses and in-flight agent runs, then stops all
        containers and cancels the idle/health loops.  Used by both the
        normal process-shutdown path and the SIGHUP restart path -- the
        difference is only whether ``startup()`` runs again afterwards.
        """
        state = self.app.state
        await wshandler.disconnect_all_websockets(state.sockets)
        await state.agents.stop_all_sessions()
        wshandler.clear_agent_mention_state()
        await state.container_registry.shutdown()

    async def process_shutdown(self) -> None:
        """Full process teardown (run once, at the very end)."""
        # instance_id() resolves from the file if startup didn't get there;
        # if there's genuinely no PID file (startup crashed early)
        # remove_pid_file no-ops on the missing file.
        state = self.app.state
        state.util.remove_pid_file()
        await state.db.dispose_engine()

    async def restart_runtime(self) -> None:
        """Graceful runtime restart: stop containers, keep the listener.

        Triggered by SIGHUP.  Before touching the runtime, configuration is
        re-resolved (``settings.reload()``, #1587); if it is invalid the
        restart is **denied** -- the running runtime is left untouched on
        its last-known-good config rather than torn down against a broken
        one.  On a valid reload the settings are **swapped** onto
        ``app.state.settings`` and the OIDC/plugins/SSL-trust/agent-user
        steps are re-run, then the runtime recycles.  All subsystems read
        settings live via ``self.app.state.settings`` (#1608), so the swap
        propagates automatically with no per-subsystem ``reconfigure()``.
        """
        if self._restart_lock is None:
            self._restart_lock = asyncio.Lock()
        async with self._restart_lock:
            new_settings, error = self._reload_settings()
            if error is not None:
                logger.error(
                    "SIGHUP: denying restart — invalid configuration: %s",
                    error,
                )
                logger.info(
                    "SIGHUP: restart denied; runtime left running on "
                    "existing configuration"
                )
                return
            logger.info("SIGHUP: applying reloaded configuration")
            await self._apply_reloaded_settings(new_settings)
            logger.info("SIGHUP: restarting runtime (keeping HTTP listener)")
            await self.runtime_shutdown()
            await self.startup()
            logger.info("SIGHUP: runtime restarted")

    def _reload_settings(
        self,
    ) -> tuple[KlangkSettings | None, str | None]:
        """Re-resolve settings for a SIGHUP reload.

        Returns ``(new, error)``: on success ``new`` is the freshly-resolved
        :class:`KlangkSettings` and ``error`` is ``None``; on failure ``new``
        is ``None`` and ``error`` is the deny reason.
        """
        try:
            new = self.app.state.settings.reload()
        except Exception as exc:  # noqa: BLE001 — surface any failure
            return None, str(exc)
        return new, None

    async def _apply_reloaded_settings(self, new: KlangkSettings) -> None:
        """Swap settings and call ``reconfigure(app_state)`` on every subsystem.

        All subsystems read ``self.app.state.settings`` live (#1608), so
        swapping the instance propagates automatically.  Each subsystem's
        ``reconfigure(app_state)`` handles any cached runtime state that
        needs refreshing (OIDC caches, plugin declarations, SSL trust,
        nginx renderer, email templates).  Most are no-ops.  Each call is
        best-effort: a failure is logged at warning level and skipped so
        one bad step can't leave the runtime half-reconfigured.
        """
        app = self.app
        old = app.state.settings
        self._warn_non_reloadable(old, new)
        app.state.settings = new

        # Every app.state subsystem that implements reconfigure().
        subsystems = [
            "ssl_trust",
            "auth",
            "podman",
            "sockets",
            "container_registry",
            "nginx_watchdog",
            "terminal",
            "oidc",
            "plugins",
            "workspaces",
            "files",
            "db",
            "model",
            "agents",
            "acl",
            "email",
            "util",
            "lifecycle",
        ]
        for name in subsystems:
            try:
                getattr(app.state, name).reconfigure(app)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SIGHUP: %s reconfigure failed (skipped): %s", name, exc
                )
        # Lifecycle.reconfigure flags an agent re-seed; apply it now
        # (async, so it can't run inside the sync reconfigure loop).
        try:
            await app.state.lifecycle.apply_pending_reseed()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SIGHUP: agent user re-seed failed (skipped): %s", exc
            )
        # #1610: remount frontend_dir if it changed.
        if old.frontend_dir != new.frontend_dir:
            self._remount_frontend(app, new)

    def _remount_frontend(self, app, settings: KlangkSettings) -> None:
        """Replace the ``/`` StaticFiles mount when ``frontend_dir`` changes."""
        # Drop the old frontend mount (by name).
        app.routes[:] = [
            r
            for r in app.routes
            if not (hasattr(r, "name") and r.name == "frontend")
        ]
        new_dir = Path(settings.frontend_dir)
        if new_dir.exists():
            setup_static_files(app, new_dir)
            logger.info("SIGHUP: frontend_dir remounted → %s", new_dir)
        else:
            logger.info(
                "SIGHUP: frontend_dir %s does not exist; UI not served",
                new_dir,
            )

    def _warn_non_reloadable(
        self, old: KlangkSettings, new: KlangkSettings
    ) -> None:
        """Log settings that changed but need a full process restart."""
        changed = [
            f"{field} ({reason})"
            for field, reason in _NON_RELOADABLE_SETTINGS
            if getattr(old, field) != getattr(new, field)
        ]
        if changed:
            logger.warning(
                "SIGHUP: settings changed but require a full process restart "
                "to take effect: %s. Restart the klangkd process to apply "
                "them.",
                "; ".join(changed),
            )

    def on_sighup(self) -> None:
        """Schedule a runtime restart on the running event loop.

        Signal callbacks can't be async, so this just creates a task.  The
        restart itself is serialized by ``_restart_lock``.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop during shutdown
            return
        loop.create_task(self.restart_runtime())


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


def enforce_no_auth_bind_safety(app) -> None:
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
    if app.state.oidc.auth_modes() != "none":
        return
    # Headless: no browser listener rendered → /auth/local not exposed on TCP.
    if app.state.settings.port is None:
        return
    host = app.state.settings.listen
    if _is_loopback_bind(host):
        return
    if app.state.settings.allow_insecure_no_auth.strip().lower() in (
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


# ---------------------------------------------------------------------------
# nginx child-process ownership (#1396, #1463)
# ---------------------------------------------------------------------------
# When the server binds a UDS (only klangkd does this), Python owns the nginx
# child: it renders nginx.conf, spawns nginx pointing at the UDS, and supervises
# it with a small async watchdog (spawn + await proc.wait() + respawn-with-
# backoff + clean SIGTERM to the process group on shutdown). No external
# supervisor library — bespoke, matching uvicorn's own precedent. devenv /
# supervisord remain only the outer restart layer for uvicorn (klangkd).


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema bootstrap: reach the DB through the single owned
    # ``app.state.db`` (wired in ``build_app``). No ambient ContextVar
    # bind — every data-access path resolves ``app.state.db`` directly
    # (#1563, #1578), which is the #1551 fix (the old env-only lazy
    # fallback that could build a different DB is gone).
    await app.state.model.init_db()
    app.state.util.resolve_instance_id()

    existing_pid = app.state.util.check_pid_file()
    if existing_pid is not None:
        logger.error(
            "Another klangk instance (PID %d) is already running "
            "for instance %s — refusing to start",
            existing_pid,
            app.state.util.instance_id(),
        )
        raise SystemExit(1)
    app.state.util.write_pid_file()

    # Make the backend process itself trust deployer-supplied CAs (#1181)
    # before any outbound TLS happens (OIDC discovery, SMTP relay, LLM-proxy
    # upstream). No-op when KLANGK_SSL_CERT_DIR is unset or empty of certs.
    app.state.ssl_trust.apply_backend_ssl_trust()

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
    enforce_no_auth_bind_safety(app)
    app.state.oidc.load_login_hook()
    await app.state.lifecycle.seed_default_user()
    await app.state.lifecycle.seed_agent_user()
    registry = app.state.container_registry

    async def _on_workspace_killed(ws_id):
        await wshandler.reset_workspace_state(app.state.sockets, ws_id)

    registry.set_on_workspace_killed(_on_workspace_killed)
    registry.set_on_container_status_changed(
        app.state.sockets.notify_container_status
    )
    await app.state.lifecycle.startup()
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
        app.state.lifecycle.on_sighup,
    )
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGHUP)
        await app.state.nginx_watchdog.stop()
        await app.state.lifecycle.runtime_shutdown()
        await app.state.lifecycle.process_shutdown()
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


# --- Live CORS middleware (#1610) ---
# Instead of a static CORSMiddleware, this wrapper re-reads allowed origins
# from app.state.util.cors_origins() on every request so a SIGHUP reload
# of KLANGK_CORS_ORIGINS takes effect without a process restart.


class LiveCORSMiddleware:
    """CORS middleware that reads allowed origins from app state on each request.

    Delegates to a ``CORSMiddleware`` instance that is rebuilt whenever the
    origin list changes.  The check-and-rebuild is O(1) most of the time
    (pointer comparison of the settings object).
    """

    def __init__(self, app_asgi, *, fastapi_app: FastAPI) -> None:
        self.app = app_asgi
        self._fastapi_app = fastapi_app
        self._last_settings = None
        self._inner: CORSMiddleware | None = None

    def _rebuild_if_needed(self) -> CORSMiddleware:
        current = self._fastapi_app.state.settings
        if current is not self._last_settings or self._inner is None:
            self._last_settings = current
            self._inner = CORSMiddleware(
                self.app,
                allow_origins=self._fastapi_app.state.util.cors_origins(),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        return self._inner

    async def __call__(self, scope, receive, send):
        inner = self._rebuild_if_needed()
        await inner(scope, receive, send)


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
    # #1567: SSLTrust(app_state) owns the settings-dependent trust surface
    # (cert-dir resolver + backend-process trust applier). The 4 pure
    # path/bundle helpers stay module-level in ssl_trust.py.
    app.state.ssl_trust = ssl_trust.SSLTrust(app)
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
    # Slice 2b (#1463): nginx watchdog is an owned instance with start/stop
    # lifecycle methods called by the lifespan.
    app.state.nginx_watchdog = nginx_mod.NginxWatchdog(app)
    # #1480: Terminal(app_state) groups the ~25 tmux-session
    # management functions that share a Podman dependency. Reaches podman,
    # the registry, and settings through the single app_state reference.
    app.state.terminal = terminal.Terminal(app)
    # #1450: OIDC(app_state) owns the provider registry, discovery/JWKS
    # caches, and login-hook state (previously module globals). Reaches
    # config through self.settings.
    app.state.oidc = oidc.OIDC(app)
    # #1451: Plugins(app_state) owns the plugins dir (computed from
    # settings, not frozen at import), declarations, and resolved values
    # (previously module globals).
    app.state.plugins = plugins.Plugins(app)
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
    # acl, workspaces, chat arrive in follow-up issues). Each reaches the
    # DB via self.app.state.db — the single instance wired just above — so
    # every code path resolves the same DB (the #1551 divergence class is
    # structurally impossible for these domains). The not-yet-converted
    # domains still go through the _current_db ContextVar backstop.
    app.state.model = model.Model(app)
    app.state.agents = agent.Agents(app)
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
    # at lifespan start (previously module-level free functions in this
    # module). The lifespan and the SIGHUP restart path call its methods.
    app.state.lifecycle = Lifecycle(app)

    app.add_middleware(LiveCORSMiddleware, fastapi_app=app)

    app.include_router(root_router)
    app.include_router(router, prefix=API_PREFIX)

    register_exception_handlers(app)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):  # pragma: no cover
        await handle_websocket(ws, app)

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
            "served. Point KLANGK_FRONTEND_DIR at a built Flutter web "
            "directory, or (for a packaged install) reinstall a wheel that "
            "ships the frontend artifact (#1600).",
            frontend_dir,
        )

    return app


# Re-export from _common so existing callers (e.g. tests) that do
# ``from klangk.main import get_app_state_dep`` keep working.
# The canonical home is ``api._common`` (avoids main <-> api circular import).
from .api._common import get_app_dep  # noqa: F401, E402


# --- ASGI app ---
# No module-level ``app = build_app(...)`` and no ``__getattr__`` shim: the
# composition root is sealed (#1454). ``klangkd`` constructs the app
# explicitly (``build_app(settings)``) and passes the object to uvicorn. The
# E2E suites launch ``e2e-tests/runtestserver.py``, which builds the app and
# passes the object to uvicorn — no ``module:app`` string import anywhere.
