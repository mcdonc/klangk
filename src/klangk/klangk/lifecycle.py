"""App lifecycle: startup/shutdown sequencing, seeding, SIGHUP restart.

Moved out of ``main.py`` in the #2738 module split; behavior is
unchanged except where noted inline (#2738 audit fixes). Owns:

- :class:`Lifecycle` — the app-level bringup/shutdown/restart sequence
  plus the default-user, agent-user, and ACL seeding that runs at
  lifespan start (#1571).
- :func:`lifespan` — the FastAPI lifespan context manager.
- :func:`setup_logfire` — opt-in Logfire instrumentation (called from
  the lifespan, after SSL trust is applied).
"""

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import (
    auth,
    fips as fips_mod,
    wshandler,
)
from . import static
from .auth import PASSWORD_CLASSES, password_class_counts
from .bind_safety import enforce_no_auth_bind_safety
from .exceptions import ConfigurationError
from .settings import KlangkSettings
from .logger import configure as configure_logging
from .model import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
from .model import AGENT_USER_ID
from .model.users import AGENT_EMAIL, AGENT_HANDLE

logger = logging.getLogger(__name__)

# Strong references to in-flight container_status broadcast tasks (#1714
# review): an unreferenced task is GC-eligible mid-execution — the same
# hazard Lifecycle's own ``_recycle_tasks`` set guards against (#2527). The
# done-callback discards from the set.
_status_broadcast_tasks: set[asyncio.Task] = set()


def broadcast_container_status(
    app, workspace_id: str, running: bool, started_at: float | None = None
) -> None:
    """Schedule the member-scoped ``container_status`` broadcast (#1714).

    Registered as the container registry's status-change callback. The
    registry invokes it synchronously (from ``track_activity`` on a
    container start and the stop paths), but the broadcast itself is
    async: it ACL-checks each recipient for workspace membership.
    Fire-and-forget on the running loop; outside a loop (sync test
    harnesses driving the registry directly) there is nothing to
    broadcast to.
    """

    async def run() -> None:
        try:
            await app.state.sockets.notify_container_status(
                workspace_id, running, started_at
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "container_status broadcast failed for workspace %s",
                workspace_id,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(run())
    _status_broadcast_tasks.add(task)
    task.add_done_callback(_status_broadcast_tasks.discard)


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
    once in :func:`klangk.main.build_app` and stored on
    ``app.state.lifecycle``, the same ``X(app_state)`` pattern every other
    owned subsystem uses (``Auth``, ``Workspaces``, ``ContainerRegistry``,
    ...). The lifespan and the SIGHUP restart path call its methods rather
    than module-level free functions; concurrent SIGHUP signals serialize
    on a per-instance lock so a second signal arriving mid-restart queues
    behind the first instead of racing.

    Pure helpers with no ``app_state`` dependency
    (:func:`klangk.bind_safety.is_loopback_bind`,
    :func:`klangk.bind_safety.enforce_no_auth_bind_safety`,
    :func:`setup_logfire`,
    :func:`klangk.main.register_exception_handlers`) stay
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
        # the signal hook in main.py before graceful_shutdown runs) and
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
        # /groups: only admins can create (#2770). Group management
        # otherwise runs through /admin/groups (admin permission).
        # Deployers can loosen this per-deployment by adding an Allow
        # `create` ACE on /groups targeting another group (the same
        # recipe as /workspaces, #2569).
        await self.app.state.model.acl.add_acl_entry(
            "/groups",
            0,
            ACTION_ALLOW,
            "create",
            PRINCIPAL_GROUP,
            group_id=admin_group_id,
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
        create) are added to this group automatically. It gets no
        default permissions — the ``/workspaces`` ``create`` seed goes to
        the admin group (#2569); deployers who want all members to
        create workspaces grant it to this group via the ACL editor.
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

        password_hash = await self._default_admin_password_hash(auth_modes)

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

    async def _default_admin_password_hash(self, auth_modes: str):
        """The default admin's password hash for the mode: hashed
        KLANGKD_DEFAULT_PASSWORD (validated against the policy, #2581,
        fail-fast) for password/both; None for none/oidc (the row exists for
        /auth/local token minting; no endpoint checks the hash)."""
        settings = self.app.state.settings
        if auth_modes not in ("password", "both"):
            return None
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
        self._validate_default_password_policy(settings, password)
        return await asyncio.to_thread(auth.hash_password, password)

    @staticmethod
    def _validate_default_password_policy(settings, password: str) -> None:
        """The default admin password must satisfy the same policy every
        other password setter enforces (#2581). Fail fast at startup —
        seeding a non-compliant password and letting it fail at first
        change would strand deployments whose policy was tightened
        after the seed ran."""
        _policy_errors = []
        min_len = int(settings.min_password_length or "8")
        if len(password) < min_len:
            _policy_errors.append(
                f"is shorter than KLANGKD_MIN_PASSWORD_LENGTH={min_len}"
            )
        _counts = password_class_counts(password)
        for _key, _name in PASSWORD_CLASSES:
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
        non-null ``password_hash``. If none do, raise
        :class:`~klangk.exceptions.ConfigurationError`.

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
        # #2738 audit: ConfigurationError (not bare RuntimeError) so the
        # launcher maps this permanent misconfiguration to EX_CONFIG (#2666)
        # instead of a generic crash a supervisor would restart-loop.
        raise ConfigurationError(
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
        handle = AGENT_HANDLE
        email = AGENT_EMAIL
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
                # #2738 audit: ConfigurationError (not bare RuntimeError) —
                # a hand-built DB a restart cannot fix; EX_CONFIG (#2666)
                # beats a supervisor restart-loop.
                raise ConfigurationError(
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
           and container reaps (so a client that reconnects and starts
           a workspace in that window is refused — 503, with the reason —
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
            new_settings, error = self.reload_settings()
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
                await self.apply_reloaded_settings(new_settings)
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

    def reload_settings(
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

    async def apply_reloaded_settings(self, new: KlangkSettings) -> None:
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
            "hooks",
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
            self.remount_frontend(app, new)

    def remount_frontend(self, app, settings: KlangkSettings) -> None:
        """Replace the frontend + branding mounts when ``frontend_dir`` changes.

        Both mounts are dropped and re-added: branding's directory is
        resolved from the (reloaded) settings at setup time, so keeping
        the old mount would keep serving a stale directory (#2738
        audit — previously only the ``frontend`` mount was replaced).
        """
        # Drop the old static mounts (by name).
        static.remove_static_mounts(app)
        new_dir = Path(settings.frontend_dir)
        if new_dir.exists():
            static.setup_static_files(app, new_dir)
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
        # #2762: customize-dir lifecycle hooks, loaded beside the login
        # hook with the same failure semantics (ConfigurationError →
        # startup_config_error → EX_CONFIG). Guarded: some minimal test
        # apps wire the lifespan without build_app's full state.
        hooks_state = getattr(app.state, "hooks", None)
        if hooks_state is not None:
            hooks_state.load_workspace_created_hook()
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
        lambda ws_id, running, started_at=None: broadcast_container_status(
            app, ws_id, running, started_at
        )
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
