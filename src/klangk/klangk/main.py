"""Klangk backend: FastAPI app with HTTP + WebSocket endpoints."""

import asyncio
import ipaddress
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    acl,
    auth,
    container,
    consent,
    emailsvc,
    files,
    fips as fips_mod,
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
from .auth import _PASSWORD_CLASSES, password_class_counts
from .exceptions import ConfigurationError
from .settings import KlangkSettings
from .logger import configure as configure_logging
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
from .model.users import (
    AGENT_EMAIL as _AGENT_EMAIL,
    AGENT_HANDLE as _AGENT_HANDLE,
)
from .wshandler import (
    handle_consent_decider,
    handle_egress_sidecar,
    handle_websocket,
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
        self._recycle_lock: asyncio.Lock | None = None
        # #2527: strong references to in-flight SIGHUP restart tasks. An
        # unreferenced task is GC-eligible mid-execution — GC between
        # ``registry.draining = True`` and the ``finally`` backstop would
        # skip the backstop and leave the node refusing every start with
        # nothing in the logs. The done-callback discards from this set.
        self._recycle_tasks: set[asyncio.Task] = set()
        # #2527: one-shot guard for the TERM/INT graceful shutdown (set by
        # the signal hook in launcher.py before graceful_shutdown runs) and
        # strong references to the hook task while it drains.
        self.shutting_down: bool = False
        self._shutdown_tasks: set[asyncio.Task] = set()

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
        # /workspaces: only admins can create (#2569)
        await self.app.state.model.acl.add_acl_entry(
            "/workspaces",
            0,
            ACTION_ALLOW,
            "create",
            PRINCIPAL_GROUP,
            group_id=admin_group_id,
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

    async def ensure_members_group(self) -> str:
        """Ensure the 'members' group exists (#2569). Returns the group ID.

        New users (registration, invitation, OIDC first login, admin
        create) are added to this group automatically. The default ACL
        seed grants ``create`` on ``/workspaces`` to this group, so
        members can create workspaces without being full admins.
        """
        group = await self.app.state.model.users.get_group_by_name("members")
        if group is None:
            group = await self.app.state.model.users.create_group(
                "members", description="All regular users"
            )
            logger.info("Created members group: %s", group["id"])
        return group["id"]

    async def seed_default_user(self) -> None:
        """Seed the default admin user exactly once, gated on admin-group
        emptiness (#1622) and the deploy's auth mode (#1645).

        Password handling depends on ``auth_modes``:

        - ``none`` / ``oidc`` → seed with ``password_hash=None``. The row is
          load-bearing for ``/auth/local`` (token minting, #1374) but no
          endpoint validates the hash — a password would be noise.
        - ``password`` / ``both`` → seed with ``KLANGKD_DEFAULT_PASSWORD``.
          **Fail-fast if unset** — auto-generating + printing to stderr was
          a footgun for detached deployments (the password was lost to a
          log nobody reads, causing lockout).

        In all modes: once the admin group has ≥1 member, this method is a
        no-op (#1622 — editing ``KLANGKD_DEFAULT_*`` and restarting cannot
        mint a new admin or clobber the existing one).
        """
        settings = self.app.state.settings
        admin_group_id = await self.ensure_admin_group()
        members_group_id = await self.ensure_members_group()
        self.app.state.members_group_id = members_group_id
        await self.seed_default_acls(admin_group_id)

        # Once an admin exists, startup must not touch users (#1622). The
        # gate is group membership, not a row id: "admin" is a group, and a
        # deployer can promote more than one admin, so keying on a fixed
        # seeded-admin id would be the wrong concept. Emptying the admin
        # group + restart re-seeds (delete-resurrection at the group level).
        members = await self.app.state.model.users.get_group_members(
            admin_group_id
        )
        if members:
            await self._enforce_password_mode_has_hashed_admin(members)
            return

        email = settings.default_user
        # Read auth_modes inline rather than via self.app.state.oidc.auth_modes()
        # so the test helper (which builds a minimal namespace without an OIDC
        # instance) can exercise seeding. Equivalent for valid inputs: the
        # settings field validator rejects typos at construction, so by here
        # auth_modes is None or one of the valid modes. None defaults to "none"
        # (the same default oidc.auth_modes() applies).
        auth_modes = settings.auth_modes or "none"

        if auth_modes in ("password", "both"):
            # Password mode: require a staged password. Fail-fast if unset —
            # auto-generation was removed to prevent lockout on detached
            # deployments (#1645).
            password = settings.default_password
            if password is None:
                raise ConfigurationError(
                    f"auth_modes={auth_modes} requires KLANGKD_DEFAULT_PASSWORD "
                    "(set it in klangkd.yaml or the env). Refusing to boot "
                    "without a known admin password."
                )
            # The default admin password must satisfy the same policy every
            # other password setter enforces (#2581). Fail fast at startup —
            # seeding a non-compliant password and letting it fail at first
            # change would strand deployments whose policy was tightened
            # after the seed ran.
            _policy_errors = []
            min_len = int(settings.min_password_length or "8")
            if len(password) < min_len:
                _policy_errors.append(
                    f"is shorter than KLANGKD_MIN_PASSWORD_LENGTH={min_len}"
                )
            _counts = password_class_counts(password)
            for _key, _name in _PASSWORD_CLASSES:
                _need = getattr(settings, f"password_require_{_key}")
                if _need > 0 and _counts[_key] < _need:
                    _policy_errors.append(
                        f"lacks the {_need} required {_name}"
                        f"{'s' if _need != 1 else ''} "
                        f"(KLANGKD_PASSWORD_REQUIRE_{_key.upper()}={_need})"
                    )
            if _policy_errors:
                raise ConfigurationError(
                    "KLANGKD_DEFAULT_PASSWORD violates the configured "
                    f"password policy: it {_policy_errors[0]}"
                    + (
                        f" (and {len(_policy_errors) - 1} more issue(s))"
                        if len(_policy_errors) > 1
                        else ""
                    )
                    + ". Fix the password or loosen the policy; refusing to "
                    "boot with a seeded admin that already violates it."
                )
            password_hash = await asyncio.to_thread(
                auth.hash_password, password
            )
        else:
            # none / oidc: seed with null password. The row exists for
            # /auth/local token minting; no endpoint checks the hash.
            password_hash = None

        user = await self.app.state.model.users.create_user(
            email, password_hash, verified=True
        )
        await self.app.state.model.users.add_user_to_group(
            user["id"], admin_group_id
        )
        if password_hash is not None:
            logger.info(
                "Created default admin user '%s' in admin group", email
            )
        else:
            logger.info(
                "Created default admin user '%s' (no password — auth_modes=%s)",
                email,
                auth_modes,
            )

    async def _enforce_password_mode_has_hashed_admin(
        self, admin_members: list[dict]
    ) -> None:
        """Refuse to boot in password/both mode if no admin can log in.

        The Table B lockout guard (#1645 review): if the admin row was seeded
        in none/oidc mode (``password_hash=None``) and the operator then flips
        to password/both, password login would 401 for every admin (``auth.py``
        treats a null hash as invalid credentials) and ``/auth/local`` is
        disabled outside none mode — so there is no path to an admin token.
        Rather than let the operator discover this at the login screen, refuse
        to boot with a clear error naming the recovery path.

        The check is: in password/both mode, at least one admin must have a
        non-null ``password_hash``. If none do, raise ``RuntimeError``.

        Recovery (documented in ``docs/features/auth-modes.md`` Table B):
        flip back to ``none`` mode, use ``/auth/local`` to get a free admin
        token, run ``klangk admin users set-password`` to set a real hash,
        then flip back to password/both. Or re-empty the admin group +
        reseed with ``KLANGKD_DEFAULT_PASSWORD`` staged.
        """
        auth_modes = self.app.state.settings.auth_modes or "none"
        if auth_modes not in ("password", "both"):
            return
        # get_group_members doesn't carry password_hash; fetch per member.
        # The admin group is small (typically 1-3 members) so the N queries
        # are cheap; a single JOIN would require a new model method for this
        # one call site, which isn't worth it.
        for member in admin_members:
            user = await self.app.state.model.users.get_user_by_email(
                member["email"]
            )
            if user and user.get("password_hash"):
                return  # At least one admin can log in — fine.
        raise RuntimeError(
            f"auth_modes={auth_modes} requires at least one admin with a "
            "password, but every admin has no password hash (seeded in "
            "none/oidc mode). Recovery: boot in none mode, run "
            "`klangk admin users set-password <email>` via /auth/local trust, "
            "then flip back to password/both. Or delete the admin row and "
            "reseed with KLANGKD_DEFAULT_PASSWORD staged."
        )

    async def seed_agent_user(self) -> None:
        """Ensure the agent user exists in the DB with the fixed identity.

        The agent *is* the klangk user (#2718): handle `klangk`, email
        `klangk@example.com` — constant, not configurable (the former
        ``KLANGKWS_FEATURE_CHAT_AGENT_EMAIL/HANDLE`` keys are gone).
        Upsert is idempotent and reconciles pre-#2718 rows (e.g. a
        `clanker`-era (pre-#2718) deployment) back to the fixed identity on every
        boot, so the migration only has to handle the colliding-human
        edge case once.

        Refuses to seed while a *human* user holds the `klangk` handle.
        A colliding agent handle is destructive:
        ``ensure_home_symlink`` would later migrate that user's home files
        into the agent's tree via its workspace-import adoption branch.
        The ``users.handle`` UNIQUE constraint is the structural backstop,
        but we fail loudly here with an actionable message instead of
        letting a bare ``IntegrityError`` abort startup mid-sequence.
        See #1137.
        """
        handle = _AGENT_HANDLE
        email = _AGENT_EMAIL
        async with self.app.state.db.transaction() as db:
            # Pre-check: refuse the fixed handle while claimed by a
            # non-agent user. The m0008 migration bumps such users to a
            # unique alternative, so this fires only if the migration
            # was skipped (e.g. a hand-built DB).
            cursor = await db.execute(
                "SELECT id FROM users WHERE handle = ? AND id != ?",
                (handle, AGENT_USER_ID),
            )
            if await cursor.fetchone() is not None:
                raise RuntimeError(
                    f"Cannot seed agent user: handle {handle!r} is already"
                    " used by another user. The m0008 migration should have"
                    " relocated it — re-run migrations or rename the user"
                    " manually."
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
        # #2342: reap containers whose creating klangkd died uncleanly (a
        # different instance whose ID no live instance matches). Tolerant of
        # label-less containers, so an older klangkd's live work is not culled.
        await registry.reap_dead_owner_containers()
        # #2527: the reaps above remove every instance-labelled container —
        # a client that reconnects after the restart's 1012 drop and starts
        # a workspace before this point would have its fresh container
        # destroyed by the reap. Keep refusing starts (the restart's drain
        # flag) until the reaps are done; boot auto-start runs after this
        # line, so it is unaffected. No-op at a genuine boot (the flag is
        # never set outside a graceful restart). A shutdown's flag is
        # never cleared here (#2527 review: the TERM path doesn't run
        # startup(), but a shutdown racing a restart must not have its
        # refusal lifted by the restart's recycle).
        if not self.shutting_down:
            registry.draining = False
        registry.start_cleanup_loop()
        registry.start_health_loop()
        registry.start_crash_loop()
        n = await state.workspaces.auto_start_workspaces()
        if n:  # pragma: no cover
            logger.info("Auto-started %d workspace(s)", n)

    async def runtime_shutdown(self) -> None:
        """Stop the runtime, keeping the HTTP listener and DB alive.

        Drops every WebSocket client (code 1012 = "reconnect"), then
        stops all containers and cancels the idle/health loops.  Used by
        both the normal process-shutdown path and the SIGHUP restart
        path -- the difference is only whether ``startup()`` runs again
        afterwards.
        """
        state = self.app.state
        await wshandler.disconnect_all_websockets(state.sockets)
        await state.container_registry.shutdown()

    async def process_shutdown(self) -> None:
        """Full process teardown (run once, at the very end)."""
        # instance_id() resolves from the file if startup didn't get there;
        # if there's genuinely no PID file (startup crashed early)
        # remove_pid_file no-ops on the missing file.
        state = self.app.state
        state.util.remove_pid_file()
        await state.db.dispose_engine()

    async def recycle_runtime(self) -> None:
        """Graceful runtime recycle: quiesce, drain, re-read config, recycle.

        Triggered by SIGHUP and by a scheduled recycle (#2661) — the
        sequence is identical either way. Each phase is logged and (the
        client-visible ones) announced as a ``server_recycle`` WebSocket
        event with a ``phase`` field; a final ``host_started`` broadcast
        closes the sequence. The HTTP listener and DB stay up the whole
        time; the process never exits.

        1. **validate** — re-resolve settings (``settings.reload()``,
           #1587). An invalid config **denies** the restart: nothing is
           touched, the runtime keeps running on its last-known-good
           config.
        2. **draining** — broadcast ``server_recycle {phase: "draining"}``
           and set the registry's in-memory drain flag so every
           container-start path refuses new starts (the single start
           choke point; the flag is never persisted, so a crashed
           restart cannot leave the node refusing starts).
        3. **quiesce** — wait up to ``quiesce_timeout`` seconds
           (default 15) for in-flight HTTP requests to finish; stragglers
           at expiry are logged and left to finish against the recycling
           runtime.
        4. **drain** — stop every running workspace through the graceful
           path (``drain_all_containers``, #2527): clients get terminal
           frames + a ``container_stopped`` event, not a dropped socket.
           Previously running workspaces are *not* remembered — only
           ``auto_start``-configured ones return with ``startup()``.
        5. **apply** — swap the reloaded settings onto
           ``app.state.settings`` and ``reconfigure()`` every subsystem
           (all read settings live, #1608).
        6. **recycle** — ``runtime_shutdown()`` then ``startup()``. The
           drain flag stays set through ``startup()``'s podman pre-warm
           and container reaps (so a client that reconnects and starts a
           workspace in that window is refused — 503, with the reason —
           instead of having its fresh container destroyed by the
           reap), and is cleared by ``startup()`` once the reaps are
           done, before auto-start runs.
        7. **resume** — broadcast ``host_started``.

        If any step raises, the exception propagates to the restart
        task's done-callback (:meth:`_on_recycle_task_done`), which logs
        it and attempts a ``startup()`` recovery; if that also fails the
        process exits (code 1) so the service manager restarts us
        rather than leaving a live-but-zombie node.
        """
        if self._recycle_lock is None:
            self._recycle_lock = asyncio.Lock()
        async with self._recycle_lock:
            state = self.app.state
            registry = state.container_registry
            logger.info("SIGHUP: restart beginning (phase: validate)")
            # #2527 review: a shutdown arriving before the restart begins
            # wins outright (on_sighup also drops later signals, but this
            # closes the checked-to-started race window).
            if self.shutting_down:
                logger.info("SIGHUP: restart aborted; shutdown in progress")
                return
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
            logger.info("SIGHUP: phase: draining (refusing new starts)")
            state.sockets.notify_server_recycle("draining")
            registry.draining = True
            try:
                # #2527 review: read the timeout from the NEW settings so
                # a reload takes effect on THIS restart, not the next.
                timeout = new_settings.quiesce_timeout
                logger.info(
                    "SIGHUP: phase: quiesce (waiting up to %.1fs for "
                    "in-flight requests)",
                    timeout,
                )
                inflight = state.inflight_requests
                idle = await inflight.wait_for_idle(timeout)
                if not idle:
                    logger.warning(
                        "SIGHUP: %d request(s) still in flight after "
                        "%.1fs; proceeding with the restart",
                        inflight.count,
                        timeout,
                    )
                logger.info("SIGHUP: phase: drain (stopping workspaces)")
                stopped = await registry.drain_all_containers(
                    reason="server recycle"
                )
                logger.info("SIGHUP: drained %d workspace(s)", stopped)
                # #2527 review: a TERM/INT landing mid-restart starts the
                # shutdown drain concurrently; from here on the restart
                # must not resurrect what the shutdown is tearing down
                # (no settings apply, no runtime recycle, no auto-start),
                # and its error recovery must not run either — the
                # process is exiting. The done-callback sees the task
                # complete normally (CancelledError would also be fine).
                if self.shutting_down:
                    logger.info(
                        "SIGHUP: restart aborted mid-drain; shutdown in "
                        "progress"
                    )
                    return
                logger.info("SIGHUP: phase: apply (applying reloaded config)")
                await self._apply_reloaded_settings(new_settings)
                state.sockets.notify_server_recycle("recycling")
                logger.info(
                    "SIGHUP: phase: restart (recycling runtime; "
                    "HTTP listener stays up)"
                )
                await self.runtime_shutdown()
                # draining deliberately stays True here: startup() clears
                # it after its container reaps (see startup()).
                await self.startup()
            finally:
                # A failed restart must never leave the node refusing
                # starts: the in-memory flag has no DB persistence an
                # operator could clear manually. EXCEPT when a shutdown
                # owns the flag now — clearing it here would lift the
                # shutdown's start-refusal while the process exits
                # (#2527 review).
                if not self.shutting_down:
                    registry.draining = False
            logger.info("SIGHUP: restart complete (phase: resumed)")
            state.sockets.notify_host_started()

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
        needs refreshing (OIDC caches, feature declarations, SSL trust,
        proxy renderer, email templates).  Most are no-ops.  Each call is
        best-effort: a failure is logged at warning level and skipped so
        one bad step can't leave the runtime half-reconfigured.
        """
        app = self.app
        old = app.state.settings
        self._warn_non_reloadable(old, new)
        app.state.settings = new
        # #1467: reconfigure global logging from the new settings *first*, so
        # any warnings the subsystem loop below emits (e.g. "ssl_trust
        # reconfigure failed") use the new KLANGKD_LOG_LEVEL. Logging is global
        # module state, reconfigured at this explicit seam (not an
        # app.state.* subsystem).
        configure_logging(new)

        # Every app.state subsystem that implements reconfigure().
        subsystems = [
            "ssl_trust",
            "netfilter",
            "auth",
            "podman",
            "sockets",
            "container_registry",
            "consent_sweeper",
            "inactivity_sweeper",
            "memory_evictor",
            "consent_coordinator",
            "consent_deciders",
            "sidecar_connections",
            "proxy_watchdog",
            "llm_router",
            "terminal",
            "oidc",
            "features",
            "workspaces",
            "files",
            "db",
            "model",
            "agents",
            "acl",
            "email",
            "util",
            "lifecycle",
            "server_scheduler",
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
        # CaddyWatchdog.reconfigure flags an admin-API POST /load of the
        # re-rendered Caddyfile; apply it now (async). No-op for the nginx
        # engine (its watchdog has no apply_pending_reload) and when the
        # proxy is disabled. #1559: a settings change is a fresh /load.
        caddy_wd = getattr(app.state, "proxy_watchdog", None)
        if caddy_wd is not None and hasattr(caddy_wd, "apply_pending_reload"):
            try:
                await caddy_wd.apply_pending_reload()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SIGHUP: caddy config reload failed (skipped): %s", exc
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

    async def graceful_shutdown(self, *, signal_num: int) -> None:
        """Graceful pre-exit work for TERM/INT (#2527): broadcast, refuse,
        quiesce, drain, then hand off to uvicorn's own exit.

        Runs at signal-receipt time (uvicorn closes every WebSocket before
        the lifespan teardown, so waiting for teardown would mean the
        ``host_shutdown`` broadcast reaches nobody). Steps, each logged:

        1. Broadcast ``host_shutdown`` so clients render "server went
           away" instead of silently reconnect-looping.
        2. Refuse new container starts (the in-memory drain flag — same
           gate as the SIGHUP restart; a start racing the shutdown gets
           a 503 instead of being killed by the process exit).
        3. Quiesce — wait up to ``quiesce_timeout`` seconds (read from
           the live settings; default 15) for in-flight HTTP requests
           to finish, so a file upload or terminal snapshot isn't cut
           off by the drain below (#2664). Stragglers at expiry are
           logged (WARNING) and left to finish against the exiting
           process — uvicorn's own in-flight wait only starts after
           this hook, when the containers are already stopped, so the
           wait has to happen here to buy them anything.
        4. Gracefully drain every running workspace — clients get
           terminal stop frames + ``container_stopped`` with reason
           ``host shutdown``, then uvicorn's 1012/exit drop lands on
           already-stopped sessions. Also denies a concurrent SIGHUP
           restart (``shutting_down`` is checked in ``on_sighup``).
        5. Call the uvicorn exit callback handed to the hook — uvicorn's
           listener stop / connection drain / lifespan teardown (which
           runs ``runtime_shutdown`` + ``process_shutdown``) takes over.

        Time-bounded by the deploy's service manager: the hook's own
        budget is the ``quiesce_timeout`` wait (default 15s) plus the
        drain's concurrent per-workspace 5s podman grace — inside
        systemd's default 90s ``TimeoutStopSec``; a second TERM/INT
        during the hook bypasses it straight to uvicorn.
        """
        state = self.app.state
        registry = state.container_registry
        name = signal.Signals(signal_num).name
        logger.info("%s: graceful shutdown beginning (phase: notify)", name)
        state.sockets.notify_host_shutdown()
        logger.info("%s: phase: draining (refusing new starts)", name)
        registry.draining = True
        # #2664: same bounded quiesce as the SIGHUP restart path, so
        # in-flight requests finish before their containers are
        # stopped. Read from the LIVE settings (no reload happens on
        # the exit path). Own try block: a quiesce failure must be
        # labeled truthfully and must not skip the drain below.
        try:
            timeout = state.settings.quiesce_timeout
            logger.info(
                "%s: phase: quiesce (waiting up to %.1fs for in-flight "
                "requests)",
                name,
                timeout,
            )
            inflight = state.inflight_requests
            idle = await inflight.wait_for_idle(timeout)
            if not idle:
                logger.warning(
                    "%s: %d request(s) still in flight after %.1fs; "
                    "proceeding with the shutdown",
                    name,
                    inflight.count,
                    timeout,
                )
        except Exception as exc:  # noqa: BLE001 — never block the exit
            logger.warning(
                "%s: quiesce failed (proceeding with shutdown): %s", name, exc
            )
        try:
            logger.info("%s: phase: drain (stopping workspaces)", name)
            stopped = await registry.drain_all_containers(
                reason="host shutdown"
            )
            logger.info("%s: drained %d workspace(s)", name, stopped)
        except Exception as exc:  # noqa: BLE001 — never block the exit
            logger.warning(
                "%s: drain failed (proceeding with exit): %s", name, exc
            )
        logger.info("%s: handing off to server exit", name)

    def on_sighup(self) -> None:
        """SIGHUP: schedule a graceful runtime restart.

        #2527: a HUP arriving after TERM/INT began the graceful shutdown
        is ignored — recycling a runtime that is being torn down would
        race the process exit (restart after drain, startup under a
        closing listener).
        """
        if self.shutting_down:
            logger.info("SIGHUP ignored: shutdown in progress")
            return
        self.request_recycle(source="SIGHUP")

    def request_recycle(self, *, source: str) -> None:
        """Schedule a graceful runtime recycle on the running event loop.

        Called by ``on_sighup`` and, with ``source="scheduled
        recycle"``, by the server scheduler when a scheduled recycle
        fires (#2661) — a scheduled recycle is the SIGHUP path, always:
        the runtime is drained and rebuilt **in-process**; the HTTP
        listener and DB stay up and the process never exits.

        Signal callbacks can't be async, so this just creates a task. The
        recycle itself is serialized by ``_recycle_lock``. The task is
        kept in ``_recycle_tasks`` (a strong reference, so the GC can
        never reap it mid-restart) and its done-callback performs
        failure recovery (#2527 review).
        """
        if self.shutting_down:
            logger.info("%s: recycle ignored; shutdown in progress", source)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop during shutdown
            return
        task = loop.create_task(self.recycle_runtime())
        self._recycle_tasks.add(task)
        task.add_done_callback(self._on_recycle_task_done)

    def _on_recycle_task_done(self, task: asyncio.Task) -> None:
        """Reap a finished restart task; recover from failure (#2527).

        A restart that raised leaves the node somewhere between drained
        and recycled (containers stopped, loops cancelled, WebSockets
        dropped) while the HTTP listener keeps serving — a zombie. Log
        the failure and try ``startup()`` once more; if that also fails,
        exit so the service manager restarts us.
        """
        self._recycle_tasks.discard(task)
        if task.cancelled():
            logger.info("SIGHUP: recycle task cancelled")
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("SIGHUP: recycle failed: %s", exc, exc_info=exc)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - loop already gone
            return
        recovery = loop.create_task(self._recover_failed_recycle())
        self._recycle_tasks.add(recovery)
        recovery.add_done_callback(self._on_recycle_task_done)

    async def _recover_failed_recycle(self) -> None:
        """Best-effort recovery after a failed graceful restart.

        Re-run ``startup()`` (idempotent by design). On success the node
        is serving and starting containers again. On failure, a live
        process that can neither restart its runtime nor serve workloads
        would masquerade as healthy — exit(1) and let systemd/docker
        restart us instead. Skipped entirely when a shutdown owns the
        process (#2527 review): resurrecting the runtime during teardown
        is exactly what the shutdown is undoing.
        """
        if self.shutting_down:
            logger.info("SIGHUP: restart recovery skipped; shutting down")
            return
        state = self.app.state
        state.container_registry.draining = False
        try:
            await self.startup()
        except Exception as exc:  # noqa: BLE001
            logger.critical(
                "SIGHUP: restart recovery failed (%s); exiting for a "
                "process restart",
                exc,
                exc_info=exc,
            )
            os._exit(1)
        logger.error(
            "SIGHUP: recycle failed but runtime recovered; "
            "configuration may be stale"
        )
        state.sockets.notify_host_started()


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

    ``KLANGKD_AUTH_MODES=none`` freely issues a token for the seeded default
    user (``POST /api/v1/auth/local``); anyone who can reach that endpoint is
    effectively logged in as admin. In full/browser mode (`KLANGKD_PORT` set),
    the loopback browser bind (`KLANGKD_LISTEN`) is the identity boundary — it
    keeps the endpoint reachable from the operator's own browser but not from
    the network or from workspace containers. Override the gate explicitly
    with ``KLANGKD_ALLOW_INSECURE_NO_AUTH=1`` when you knowingly expose a
    no-auth server (e.g. a throwaway VM on an isolated network). #1374.

    In headless mode (`KLANGKD_PORT` unset) there is no browser listener at
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
            "KLANGKD_AUTH_MODES=none with non-loopback bind %r — allowed "
            "because KLANGKD_ALLOW_INSECURE_NO_AUTH=1. Anyone who can reach "
            "this address is effectively logged in as the default admin user.",
            host,
        )
        return
    raise ConfigurationError(
        "Refusing to start: KLANGKD_AUTH_MODES=none but KLANGKD_LISTEN=%r "
        "is not a loopback address. no-auth mode freely issues an admin "
        "token, so it must bind loopback (127.0.0.0/8, ::1, or localhost). "
        "Set KLANGKD_LISTEN=127.0.0.1, or set KLANGKD_ALLOW_INSECURE_NO_AUTH=1 "
        "to override if you understand the risk. See #1374." % host
    )


# ---------------------------------------------------------------------------
# proxy child-process ownership (#1396, #1463)
# ---------------------------------------------------------------------------
# When the server binds a UDS (only klangkd does this), Python owns the proxy
# child (currently nginx): it renders nginx.conf, spawns nginx pointing at the
# UDS, and supervises it with a small async watchdog (spawn + await proc.wait()
# + respawn-with- backoff + clean SIGTERM to the process group on shutdown). No
# external supervisor library — bespoke, matching uvicorn's own precedent.
# devenv / supervisord remain only the outer restart layer for uvicorn (klangkd).


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
    # upstream). No-op when <KLANGKD_CUSTOMIZE_DIR>/certs/ is absent or empty of certs.
    app.state.ssl_trust.apply_backend_ssl_trust()

    # Configure Logfire *after* SSL trust is applied. logfire.configure()
    # probes the Logfire API at configuration time, so it must run once the
    # SSL_* env vars (pointing at the merged CA bundle) are set, or it
    # emits an unreachable-API warning against a private-CA endpoint (#1406).
    # Was previously called at module scope, which runs before this lifespan
    # and therefore before trust is applied.
    setup_logfire(app)

    # Deterministic config validation + seeding. A ConfigurationError from
    # this stretch is a config problem a restart cannot fix; flag it on
    # app.state so the launcher can exit EX_CONFIG (78) instead of uvicorn's
    # generic startup-failure status — without that, a supervisor
    # restart-loops a bad password/secret/bind forever (#2666).
    try:
        app.state.auth.require_secure_jwt_secret()
        # #2570: with KLANGKD_FIPS_MODE on, audit klangkd's own OpenSSL (its
        # password hashing + JWT signing) once at startup — verified, or a
        # prominent warning (klangkd may legitimately run on a non-FIPS
        # control host; workspace containers are the fail-closed gate).
        fips_mod.verify_process_fips(app.state.settings)
        # Features reads the build-emitted features.json at construction
        # (Features(app) in build_app); no separate load() step (#1655).
        app.state.oidc.init_providers()
        enforce_no_auth_bind_safety(app)
        app.state.oidc.load_login_hook()
        await app.state.lifecycle.seed_default_user()
        await app.state.lifecycle.seed_agent_user()
    except ConfigurationError as exc:
        app.state.startup_config_error = str(exc)
        raise
    registry = app.state.container_registry

    async def _on_workspace_killed(ws_id, container_id=None):
        await wshandler.reset_workspace_state(
            app.state.sockets, ws_id, expected_container_id=container_id
        )

    registry.set_on_workspace_killed(_on_workspace_killed)
    registry.set_on_container_status_changed(
        app.state.sockets.notify_container_status
    )
    await app.state.lifecycle.startup()
    # Reap orphaned pending consent rows from a prior run: the in-memory
    # holds are gone on restart, so without this the decider snapshot would
    # replay stale requests that can never be resolved (#2310).
    reaped = await app.state.model.egress_consent.expire_all_pending()
    logger.info(
        "expired %d orphaned pending egress-consent request(s) on startup",
        reaped,
    )
    app.state.consent_sweeper.start()
    app.state.inactivity_sweeper.start()
    app.state.consent_coordinator.start()
    app.state.consent_deciders.start()
    app.state.sidecar_connections.start()
    # #2526: host memory-pressure eviction loop (k8s node-pressure-eviction
    # analogue) — stops idle workspaces before the kernel OOM killer picks a
    # random victim (possibly klangkd itself).
    app.state.memory_evictor.start()
    # #2661: scheduled server stop/recycle loop — fires persisted
    # schedules (surviving this daemon's restarts) and keeps every
    # client informed with the pending-schedule snapshot. Guarded: some
    # minimal test apps wire the lifespan without build_app's full state.
    server_scheduler = getattr(app.state, "server_scheduler", None)
    if server_scheduler is not None:
        server_scheduler.start()
    # Start the proxy (only when bound to a UDS — klangkd; no-op for TCP tests).
    # Rendered + owned by Python (#1396); replaces scripts/nginx.sh.
    await app.state.proxy_watchdog.start()
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
        await app.state.consent_sweeper.stop()
        server_scheduler = getattr(app.state, "server_scheduler", None)
        if server_scheduler is not None:
            await server_scheduler.stop()
        await app.state.inactivity_sweeper.stop()
        await app.state.memory_evictor.stop()
        await app.state.consent_coordinator.stop()
        await app.state.consent_deciders.stop()
        await app.state.sidecar_connections.stop()
        await app.state.proxy_watchdog.stop()
        await app.state.lifecycle.runtime_shutdown()
        await app.state.lifecycle.process_shutdown()
        logger.info("Klangk backend stopped")


def setup_logfire(app: FastAPI) -> bool:
    """Enable Logfire instrumentation if LOGFIRE_TOKEN is set."""
    if not os.environ.get("LOGFIRE_TOKEN"):
        return False
    import logfire  # allow-deferred-import (opt-in, ~440ms)

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
# of KLANGKD_CORS_ORIGINS takes effect without a process restart.


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


class InFlightRequests:
    """In-flight HTTP request counter (#2527 graceful restart/shutdown).

    Backs the quiesce phase of both the SIGHUP restart and the
    TERM/INT shutdown: after new container starts are refused,
    :meth:`wait_for_idle` waits for the request count to reach zero
    before the containers are drained. Not an owned subsystem —
    a plain counter with no app dependency.
    """

    def __init__(self) -> None:
        self.count = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def increment(self) -> None:
        if self.count == 0:
            self._idle.clear()
        self.count += 1

    def decrement(self) -> None:
        self.count = max(0, self.count - 1)
        if self.count == 0:
            self._idle.set()

    async def wait_for_idle(self, timeout: float) -> bool:
        """Wait until no requests are in flight; False on timeout."""
        if self.count == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True


class InFlightMiddleware:
    """Pure-ASGI wrapper counting in-flight ``http`` requests (#2527).

    ``http`` scopes only — a WebSocket connection never "completes", so
    counting it would block the drain quiesce forever. The counter is
    shared via ``app.state.inflight_requests`` so the SIGHUP restart
    and TERM/INT shutdown paths can wait on it.
    """

    def __init__(self, app_asgi, counter: InFlightRequests) -> None:
        self.app = app_asgi
        self.counter = counter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.counter.increment()
        try:
            await self.app(scope, receive, send)
        finally:
            self.counter.decrement()


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority


def setup_static_files(app: FastAPI, frontend_dir: Path) -> None:
    """Mount Flutter Web static files and add no-cache middleware.

    Optionally mounts a branding directory at ``/branding`` so a custom
    logo / assets can be served without a Flutter rebuild.  Prefers
    ``<KLANGKD_CUSTOMIZE_DIR>/branding`` when it exists; falls back to
    ``<KLANGKD_DATA_DIR>/branding`` if that exists.  If neither directory
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
    # at lifespan start (previously module-level free functions in this
    # module). The lifespan and the SIGHUP restart path call its methods.
    app.state.lifecycle = Lifecycle(app)

    app.add_middleware(InFlightMiddleware, counter=app.state.inflight_requests)
    app.add_middleware(LiveCORSMiddleware, fastapi_app=app)

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
