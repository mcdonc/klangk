"""Tests for main.py: lifespan, seed user, static files, logfire."""

import asyncio
import os
import signal
import sqlite3
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk import (
    agent as agent_mod,
    auth as auth_mod,
    emailsvc as emailsvc_mod,
    files as files_mod,
    nginx as nginx_mod,
    ssl_trust as ssl_trust_mod,
    util as util_mod,
    main,
    model,
    oidc,
    plugins,
    workspaces,
)
from klangk.container import ContainerRegistry
from _helpers import make_settings
from klangk.wshandler.session import WebSocketState


def _make_app_state(settings=None):
    """Build a minimal mock app for tests."""
    if settings is None:
        # Pin a default password so the lifespan's seed_default_user does
        # not generate (and print) one to stderr on every boot (#1493).
        settings = make_settings({"KLANGK_DEFAULT_PASSWORD": "test"})
    # Two-phase: shell first so owned instances can take app at
    # construction (#1426).
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    sockets = WebSocketState(app_state)
    app_state.state.sockets = sockets
    registry = ContainerRegistry(app_state)
    app_state.state.container_registry = registry
    # #1468: container.py / agent.py reach the CLI wrappers via self.podman.
    from klangk.podman import Podman

    app_state.state.podman = Podman(app_state)
    app_state.state.oidc = oidc.OIDC(app_state)
    app_state.state.plugins = plugins.Plugins(app_state)
    app_state.state.workspaces = workspaces.Workspaces(app_state)
    app_state.state.files = files_mod.Files(app_state)
    # #1520: the lifespan binds app.state.db as the active DB for its context;
    # mirror build_app so lifespan-driven tests have it.
    from klangk.model import db as db_mod

    app_state.state.db = db_mod.DB(app_state)
    # #1572: Model(app_state) composing the converted domains.
    from klangk.model import Model

    app_state.state.model = Model(app_state)
    app_state.state.agents = agent_mod.Agents(app_state)
    app_state.state.email = emailsvc_mod.EmailService(app_state)
    app_state.state.util = util_mod.Util(app_state)
    # #1567: the lifespan calls app.state.ssl_trust.apply_backend_ssl_trust().
    app_state.state.ssl_trust = ssl_trust_mod.SSLTrust(app_state)
    app_state.state.auth = auth_mod.Auth(app_state)
    app_state.state.nginx_watchdog = nginx_mod.NginxWatchdog(app_state)
    from klangk.terminal import Terminal
    from klangk.acl import ACL

    app_state.state.terminal = Terminal(app_state)
    app_state.state.acl = ACL(app_state)
    # #1571: Lifecycle(app_state) owns startup/shutdown/restart + seeding.
    app_state.state.lifecycle = main.Lifecycle(app_state)
    return app_state


def _lifecycle(settings):
    """A ``Lifecycle`` whose app can reach ``model.acl``.

    The seed methods read ``self.app.state.settings`` and reach the DB via
    ``self.app.state.model.acl.*`` (ACL seeding), so the namespace needs
    ``db`` + ``model`` wired (#1574). ``wire_db_and_model`` reuses the
    per-test DB.
    """
    from _helpers import wire_db_and_model

    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    wire_db_and_model(app)
    return main.Lifecycle(app)


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGK_DEFAULT_USER": "seed-test",
                    "KLANGK_DEFAULT_PASSWORD": "seed-pass",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, app_state):
        s = make_settings(
            {
                "KLANGK_DEFAULT_USER": "seed-test",
                "KLANGK_DEFAULT_PASSWORD": "seed-pass",
            }
        )
        await _lifecycle(s).seed_default_user()
        # Call again — should not raise
        await _lifecycle(s).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is not None

    async def test_generates_password_when_not_set(
        self, db, capsys, app_state
    ):
        await _lifecycle(
            make_settings({"KLANGK_DEFAULT_USER": "gen-test"})
        ).seed_default_user()
        # Swallow the incidental generated-password print to stderr
        # (asserted explicitly by test_generated_password_printed_to_stderr).
        capsys.readouterr()
        user = await app_state.state.model.users.get_user_by_email("gen-test")
        assert user is not None
        # User exists and is in the admin group
        admin_group = await app_state.state.model.users.get_group_by_name(
            "admin"
        )
        assert admin_group is not None
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert admin_group["id"] in group_ids

    async def test_generated_password_printed_to_stderr(
        self, db, caplog, capsys
    ):
        import logging

        with caplog.at_level(logging.INFO):
            await _lifecycle(
                make_settings({"KLANGK_DEFAULT_USER": "log-test"})
            ).seed_default_user()
        # Password must NOT appear in log output (security)
        assert "password printed to stderr" in caplog.text
        assert "log-test" in caplog.text
        # Password appears on stderr only
        captured = capsys.readouterr()
        assert "Default admin password for" in captured.err


# --- no-auth bind safety gate (#1374) ---


def _bind_safety_app_state(
    auth_mode=None, listen=None, allow_insecure=None, port="8997"
):
    """Build a minimal app_state whose oidc reads the given auth mode (#1450).

    Pass the mode/listen/allow-insecure explicitly — the bind-safety tests
    exercise different combinations, and these are now read from
    ``settings`` frozen at construction (#1518) instead of re-reading the
    env per call. ``port`` defaults to ``"8997"`` (full/browser mode) so the
    browser-bind gate applies; pass ``port=None`` to exercise headless
    (where the gate is a no-op — no browser listener, #1542).
    """
    env = {"KLANGK_AUTH_MODES": auth_mode} if auth_mode else {}
    if port is not None:
        env["KLANGK_PORT"] = port
    if listen is not None:
        env["KLANGK_LISTEN"] = listen
    if allow_insecure is not None:
        env["KLANGK_ALLOW_INSECURE_NO_AUTH"] = allow_insecure
    settings = make_settings(env)
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app_state.state.oidc = oidc.OIDC(app_state)
    app_state.state.plugins = plugins.Plugins(app_state)
    app_state.state.workspaces = workspaces.Workspaces(app_state)
    return app_state


class TestNoAuthBindSafety:
    """enforce_no_auth_bind_safety() — refuse none mode on a non-loopback
    bind unless explicitly overridden."""

    def test_noop_when_not_none_mode(self):
        # Returns None, raises nothing.
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="password", listen="0.0.0.0")
            )
            is None
        )

    def test_allows_loopback_ipv4(self):
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="127.0.0.1")
            )
            is None
        )

    def test_allows_loopback_ipv6_and_localhost(self):
        for host in ("::1", "localhost"):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(auth_mode="none", listen=host)
                )
                is None
            )

    def test_allows_full_loopback_range(self):
        """The whole 127.0.0.0/8 range is loopback (RFC 990), not just
        127.0.0.1 — ``127.0.0.2`` is a valid loopback bind and must be
        admitted (the original exact-match allowlist wrongly refused it)."""
        for host in ("127.0.0.2", "127.255.255.254"):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(auth_mode="none", listen=host)
                )
                is None
            )

    def test_allows_loopback_default_when_listen_unset(self):
        # KLANGK_LISTEN defaults to 127.0.0.1 (#1375).
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none")
            )
            is None
        )

    def test_refuses_ipv6_wildcard(self):
        """``::`` binds every interface (incl. IPv6) and is NOT loopback —
        must be refused even though it isn't ``0.0.0.0``."""
        with pytest.raises(SystemExit) as exc_info:
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="::")
            )
        assert "::" in str(exc_info.value)

    def test_refuses_non_loopback_hostname(self):
        """A bare hostname (other than ``localhost``) is not an IP literal and
        not a recognized loopback name — fail-closed (refuse)."""
        with pytest.raises(SystemExit):
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="myhost")
            )

    def test_refuses_non_loopback_bind(self):
        with pytest.raises(SystemExit) as exc_info:
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="0.0.0.0")
            )
        msg = str(exc_info.value)
        assert "KLANGK_AUTH_MODES=none" in msg
        assert "loopback" in msg
        assert "KLANGK_ALLOW_INSECURE_NO_AUTH=1" in msg
        assert "0.0.0.0" in msg

    def test_headless_exempt_from_bind_check(self):
        """Headless (KLANGK_PORT unset) has no browser listener, so the bind
        gate is a no-op — none mode is safe regardless of the listen address,
        because /auth/local is never exposed over TCP (#1542)."""
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(
                    auth_mode="none", listen="0.0.0.0", port=None
                )
            )
            is None
        )

    def test_override_flag_allows_non_loopback(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(
                        auth_mode="none",
                        listen="0.0.0.0",
                        allow_insecure="1",
                    )
                )
                is None
            )
        assert "non-loopback bind" in caplog.text


# --- Seed agent user ---


class TestSeedAgentUser:
    async def test_creates_agent_user(self, db, app_state):
        await _lifecycle(make_settings({})).seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user is not None
        assert user["email"] == "clanker@example.com"
        assert user["handle"] == "clanker"

    async def test_custom_env_vars(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGK_AGENT_EMAIL": "bot@test.com",
                    "KLANGK_AGENT_HANDLE": "TestBot",
                }
            )
        ).seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user is not None
        assert user["email"] == "bot@test.com"
        assert user["handle"] == "TestBot"

    async def test_upserts_existing(self, db, app_state):
        await _lifecycle(make_settings({})).seed_agent_user()
        await _lifecycle(
            make_settings(
                {
                    "KLANGK_AGENT_EMAIL": "new@test.com",
                    "KLANGK_AGENT_HANDLE": "NewBot",
                }
            )
        ).seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user["email"] == "new@test.com"
        assert user["handle"] == "NewBot"

    async def test_clears_cache(self, db, app_state):
        # Prime cache with fallback
        await app_state.state.model.users.get_agent_user()
        await _lifecycle(make_settings({})).seed_agent_user()
        # Cache should now reflect DB values
        agent = await app_state.state.model.users.get_agent_user()
        assert agent["email"] == "clanker@example.com"

    async def test_users_handle_has_unique_constraint(self, db, app_state):
        """The users.handle UNIQUE constraint is the structural backstop.

        Confirms a duplicate handle raises IntegrityError at the DB layer,
        independent of seed_agent_user's pre-check.  See #1137.
        """
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "INSERT INTO users (id, email, handle)"
                " VALUES ('uid-a', 'a@x.com', 'alice')"
            )
            with pytest.raises(SAIntegrityError) as exc_info:
                await db_conn.execute(
                    "INSERT INTO users (id, email, handle)"
                    " VALUES ('uid-b', 'b@x.com', 'alice')"
                )
        # The underlying driver-level cause is the sqlite UNIQUE violation.
        assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)

    async def test_seed_refuses_handle_collision_with_human(
        self, db, app_state
    ):
        """Seeding the agent with a live user's handle fails cleanly.

        The destructive path is ensure_home_symlink migrating that user's
        files into the agent's tree; the guard must abort before any such
        work.  See #1137.
        """
        human = await app_state.state.model.users.create_user(
            "alice@example.com", "hash", verified=True
        )
        assert human["handle"] == "alice"
        with pytest.raises(RuntimeError, match="alice"):
            await _lifecycle(
                make_settings({"KLANGK_AGENT_HANDLE": "alice"})
            ).seed_agent_user()
        # Human user is untouched.
        refreshed = await app_state.state.model.users.get_user_by_id(
            human["id"]
        )
        assert refreshed["handle"] == "alice"
        # Agent was not created with the colliding handle.
        assert (
            await app_state.state.model.users.get_user_by_id(
                model.AGENT_USER_ID
            )
            is None
        )

    async def test_seed_rename_to_human_handle_refuses(self, db, app_state):
        """Re-seeding the agent onto a human's handle fails, leaves agent as-is."""
        await _lifecycle(
            make_settings({})
        ).seed_agent_user()  # agent handle = clanker
        human = await app_state.state.model.users.create_user(
            "alice@example.com", "hash", verified=True
        )
        with pytest.raises(RuntimeError, match="already used by another user"):
            await _lifecycle(
                make_settings({"KLANGK_AGENT_HANDLE": "alice"})
            ).seed_agent_user()
        # Agent keeps its original handle; human untouched.
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert agent["handle"] == "clanker"
        refreshed = await app_state.state.model.users.get_user_by_id(
            human["id"]
        )
        assert refreshed["handle"] == "alice"

    async def test_collision_leaves_human_files_untouched(
        self, db, tmp_path, app_state
    ):
        """A handle collision never reaches ensure_home_symlink's adoption.

        Builds the on-disk layout that the destructive branch would migrate
        (a /home/<handle> symlink -> .users/<human-uid> with files) and
        confirms a colliding agent seed aborts before any file moves.  See
        #1137.
        """
        human = await app_state.state.model.users.create_user(
            "alice@example.com", "hash", verified=True
        )
        # Stand up the destructive-branch precondition directly on disk.
        home = tmp_path / "home"
        users_dir = home / ".users"
        users_dir.mkdir(parents=True)
        human_dir = users_dir / human["id"]
        human_dir.mkdir()
        (human_dir / "secret.txt").write_text("alice's secrets")
        (home / "alice").symlink_to(f".users/{human['id']}")

        with pytest.raises(RuntimeError):
            await _lifecycle(
                make_settings({"KLANGK_AGENT_HANDLE": "alice"})
            ).seed_agent_user()

        # Human's files are exactly where they were — nothing migrated.
        assert (human_dir / "secret.txt").read_text() == "alice's secrets"
        assert os.readlink(home / "alice") == f".users/{human['id']}"
        # No agent user directory was created.
        assert not (users_dir / model.AGENT_USER_ID).exists()


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db, app_state):
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.nginx_watchdog = nginx_mod.NginxWatchdog(app)
        app.state.oidc = oidc.OIDC(app)
        app.state.plugins = plugins.Plugins(app)
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.agents = agent_mod.Agents(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(registry, "start_cleanup_loop") as mock_start,
            patch.object(
                registry,
                "shutdown",
                new_callable=AsyncMock,
            ) as mock_shutdown,
            patch.object(
                util_mod.Util, "check_pid_file", return_value=None
            ) as mock_check,
            patch.object(util_mod.Util, "write_pid_file") as mock_write,
            patch.object(util_mod.Util, "remove_pid_file") as mock_remove,
        ):
            async with main.lifespan(app):
                mock_check.assert_called_once()
                mock_write.assert_called_once()
                mock_adopt.assert_awaited_once()
                mock_start.assert_called_once()
            mock_shutdown.assert_awaited_once()
            mock_remove.assert_called_once()

    async def test_lifespan_workspace_killed_resets_state(self, db, app_state):
        """The workspace-killed callback threads app.state into
        reset_workspace_state (sockets, workspace_id) — #1475."""
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.nginx_watchdog = nginx_mod.NginxWatchdog(app)
        app.state.oidc = oidc.OIDC(app)
        app.state.plugins = plugins.Plugins(app)
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.agents = agent_mod.Agents(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "shutdown", new_callable=AsyncMock),
            patch.object(util_mod.Util, "check_pid_file", return_value=None),
            patch.object(util_mod.Util, "write_pid_file"),
            patch.object(util_mod.Util, "remove_pid_file"),
            patch(
                "klangk.main.wshandler.reset_workspace_state",
                new_callable=AsyncMock,
            ) as mock_reset,
        ):
            async with main.lifespan(app):
                # The closure registered by the lifespan threads app.state
                # into reset_workspace_state: (sockets, workspace_id).
                assert registry.on_workspace_killed is not None
                await registry.on_workspace_killed("ws-killed")
        mock_reset.assert_awaited_once_with(app.state.sockets, "ws-killed")

    async def test_lifespan_refuses_if_pid_alive(self, db, app_state):

        app = FastAPI()
        app_state = _make_app_state()
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.util = util_mod.Util(app)
        # The lifespan reaches the DB through ``app.state.db`` +
        # ``app.state.model`` (no ContextVar bind post-#1578); point both at
        # the test-built app_state so init_db runs before the pid refuse.
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        with (
            patch.object(util_mod.Util, "check_pid_file", return_value=12345),
            pytest.raises(SystemExit),
        ):
            async with main.lifespan(app):
                pass  # pragma: no cover


# --- SIGHUP runtime restart (#1212) ---


class TestStartupShutdownRestart:
    async def test_startup_calls_container_sequence(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "prewarm_podman",
                new_callable=AsyncMock,
            ) as mock_prewarm,
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(registry, "start_cleanup_loop") as mock_cleanup,
            patch.object(registry, "start_health_loop") as mock_health,
            patch.object(
                workspaces.Workspaces,
                "auto_start_workspaces",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_autostart,
        ):
            await app_state.state.lifecycle.startup()
        mock_prewarm.assert_awaited_once()
        mock_adopt.assert_awaited_once()
        mock_cleanup.assert_called_once()
        mock_health.assert_called_once()
        mock_autostart.assert_awaited_once()

    async def test_runtime_shutdown_tears_down_layers(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        with (
            patch(
                "klangk.main.wshandler.disconnect_all_websockets",
                new_callable=AsyncMock,
            ) as mock_disc,
            patch.object(
                app_state.state.agents,
                "stop_all_sessions",
                new_callable=AsyncMock,
            ) as mock_stop_agents,
            patch(
                "klangk.main.wshandler.clear_agent_mention_state"
            ) as mock_clear,
            patch.object(
                registry, "shutdown", new_callable=AsyncMock
            ) as mock_shutdown,
        ):
            await app_state.state.lifecycle.runtime_shutdown()
        mock_disc.assert_awaited_once()
        mock_stop_agents.assert_awaited_once()
        mock_clear.assert_called_once()
        mock_shutdown.assert_awaited_once()

    async def test_process_shutdown_disposes(self, app_state):
        app_state = _make_app_state()
        app_state.state.db = AsyncMock()
        with (
            patch.object(util_mod.Util, "remove_pid_file") as mock_remove,
        ):
            await app_state.state.lifecycle.process_shutdown()
        mock_remove.assert_called_once()
        app_state.state.db.dispose_engine.assert_awaited_once()

    async def test_restart_runtime_runs_shutdown_then_startup(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._restart_lock = None  # force fresh lock creation
        with (
            patch.object(
                lc, "runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch.object(lc, "startup", new_callable=AsyncMock) as mock_up,
        ):
            await lc.restart_runtime()
        mock_down.assert_awaited_once()
        mock_up.assert_awaited_once()
        # Lock was created and is now held-free.
        assert lc._restart_lock is not None

    async def test_restart_runtime_reuses_existing_lock(self, app_state):
        # Seed a lock explicitly; ``restart_runtime`` must reuse it rather
        # than create a new one. The lock is now per-instance (#1571), so a
        # fresh Lifecycle starts at the pre-first-use floor without a
        # cross-test reset fixture.
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._restart_lock = asyncio.Lock()
        existing = lc._restart_lock
        with (
            patch.object(lc, "runtime_shutdown", new_callable=AsyncMock),
            patch.object(lc, "startup", new_callable=AsyncMock),
        ):
            await lc.restart_runtime()
        # Same lock object reused, not replaced.
        assert lc._restart_lock is existing

    async def test_restart_lock_serializes_concurrent_calls(self, app_state):
        """Two restarts kicked off together run strictly one-after-another."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._restart_lock = None
        order = []

        async def fake_shutdown():
            order.append("down-start")
            await asyncio.sleep(0.01)
            order.append("down-end")

        async def fake_startup():
            order.append("up")

        with (
            patch.object(lc, "runtime_shutdown", side_effect=fake_shutdown),
            patch.object(lc, "startup", side_effect=fake_startup),
        ):
            await asyncio.gather(
                lc.restart_runtime(),
                lc.restart_runtime(),
            )
        # Two complete down-start...down-end...up cycles, never interleaved.
        assert order == [
            "down-start",
            "down-end",
            "up",
            "down-start",
            "down-end",
            "up",
        ]

    async def test_restart_denies_on_invalid_config(self, app_state):
        """Invalid config denies the restart; no teardown, no startup."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._restart_lock = None
        with (
            patch.object(
                lc,
                "_reload_settings",
                return_value=(None, "bad config"),
            ) as mock_reload,
            patch.object(
                lc, "runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch.object(lc, "startup", new_callable=AsyncMock) as mock_up,
        ):
            await lc.restart_runtime()
        mock_reload.assert_called_once()
        mock_down.assert_not_awaited()
        mock_up.assert_not_awaited()

    async def test_restart_reloads_then_applies_then_restarts(self, app_state):
        """Valid config: reload → apply → shutdown → startup."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._restart_lock = None
        new_settings = make_settings({"KLANGK_DEFAULT_PASSWORD": "test"})
        order = []
        with (
            patch.object(
                lc,
                "_reload_settings",
                return_value=(new_settings, None),
            ),
            patch.object(
                lc,
                "_apply_reloaded_settings",
                new_callable=AsyncMock,
                side_effect=lambda s: order.append("apply"),
            ),
            patch.object(
                lc,
                "runtime_shutdown",
                new_callable=AsyncMock,
                side_effect=lambda: order.append("shutdown"),
            ),
            patch.object(
                lc,
                "startup",
                new_callable=AsyncMock,
                side_effect=lambda: order.append("startup"),
            ),
        ):
            await lc.restart_runtime()
        assert order == ["apply", "shutdown", "startup"]

    def test_reload_settings_returns_new_when_valid(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new, error = lc._reload_settings()
        assert new is not None
        assert error is None
        assert new is not app_state.state.settings

    def test_reload_settings_returns_error_when_invalid(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        # Pydantic models can't be patched with patch.object; patch the
        # class method instead.
        with patch.object(
            type(app_state.state.settings),
            "reload",
            side_effect=ValueError("bad"),
        ):
            new, error = lc._reload_settings()
        assert new is None
        assert "bad" in error

    async def test_apply_reloaded_settings_calls_reconfigure(self, app_state):
        """Swap + reconfigure called on every subsystem."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGK_DEFAULT_PASSWORD": "test"})
        old_settings = app_state.state.settings
        called = []
        for attr in (
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
        ):
            obj = getattr(app_state.state, attr)
            orig = obj.reconfigure

            def make_tracker(name, orig_fn):
                def tracked(app):
                    called.append(name)
                    return orig_fn(app)

                return tracked

            obj.reconfigure = make_tracker(attr, orig)
        with patch.object(
            lc, "apply_pending_reseed", new_callable=AsyncMock
        ) as mock_reseed:
            await lc._apply_reloaded_settings(new_settings)
        assert app_state.state.settings is new_settings
        assert app_state.state.settings is not old_settings
        assert "ssl_trust" in called
        assert "oidc" in called
        assert "plugins" in called
        assert len(called) == 18
        mock_reseed.assert_awaited_once()

    async def test_apply_logs_warning_when_reconfigure_fails(
        self, app_state, caplog
    ):
        """A failing reconfigure is skipped + warned, the rest still run."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGK_DEFAULT_PASSWORD": "test"})
        with (
            patch.object(
                app_state.state.ssl_trust,
                "reconfigure",
                side_effect=RuntimeError("ssl boom"),
            ),
            patch.object(
                app_state.state.oidc, "reconfigure"
            ) as mock_oidc_reconf,
            patch.object(
                lc, "apply_pending_reseed", new_callable=AsyncMock
            ) as mock_reseed,
            caplog.at_level("WARNING"),
        ):
            await lc._apply_reloaded_settings(new_settings)
        assert "ssl_trust reconfigure failed" in caplog.text
        mock_oidc_reconf.assert_called_once()
        mock_reseed.assert_awaited_once()

    def test_warn_non_reloadable_logs_changed_settings(
        self, app_state, caplog
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old = app_state.state.settings
        new = make_settings(
            {"KLANGK_DEFAULT_PASSWORD": "test", "KLANGK_PORT": "9999"}
        )
        with caplog.at_level("WARNING"):
            lc._warn_non_reloadable(old, new)
        assert "port" in caplog.text
        assert "full process restart" in caplog.text

    async def test_apply_pending_reseed_noop_without_flag(self, app_state):
        """apply_pending_reseed is a no-op when reconfigure hasn't been called."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with patch.object(
            lc, "seed_agent_user", new_callable=AsyncMock
        ) as mock_seed:
            await lc.apply_pending_reseed()
        mock_seed.assert_not_awaited()

    def test_warn_non_reloadable_silent_on_reloadable_only(
        self, app_state, caplog
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old = app_state.state.settings
        # Build new settings from the same env as old so only
        # reloadable fields differ.
        env = dict(old._reload_env)
        env["KLANGK_AGENT_HANDLE"] = "newbot"
        new = make_settings(env)
        with caplog.at_level("WARNING"):
            lc._warn_non_reloadable(old, new)
        assert "full process restart" not in caplog.text

    async def test_agent_handle_change_takes_effect_after_restart(
        self, db, app_state
    ):
        """Acceptance test: editing KLANGK_AGENT_HANDLE + SIGHUP makes the
        new handle the live agent handle without a process restart."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle

        # Seed the initial agent user using the test DB.
        from _helpers import get_test_db

        app_state.state.db = get_test_db()
        app_state.state.model = model.Model(app_state)
        await lc.seed_agent_user()
        old_handle = await app_state.state.model.users.agent_handle()
        assert old_handle == "clanker"

        # Simulate a config change: new settings with a different handle.
        env = dict(app_state.state.settings._reload_env)
        env["KLANGK_AGENT_HANDLE"] = "newbot"
        env["KLANGK_AGENT_EMAIL"] = "newbot@example.com"
        new_settings = make_settings(env)
        await lc._apply_reloaded_settings(new_settings)
        new_handle = await app_state.state.model.users.agent_handle()
        assert new_handle == "newbot"

    async def test_on_sighup_schedules_restart(self, app_state):
        """on_sighup creates a task that runs restart_runtime."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with patch.object(
            lc, "restart_runtime", new_callable=AsyncMock
        ) as mock_restart:
            lc.on_sighup()
            # Let the scheduled task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        mock_restart.assert_awaited_once()

    async def test_lifespan_registers_sighup_handler(self, db, app_state):
        """The lifespan installs (and removes) a SIGHUP handler."""
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.nginx_watchdog = nginx_mod.NginxWatchdog(app)
        app.state.oidc = oidc.OIDC(app)
        app.state.plugins = plugins.Plugins(app)
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.agents = agent_mod.Agents(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        loop = asyncio.get_running_loop()
        with (
            patch.object(
                loop, "add_signal_handler", new_callable=MagicMock
            ) as mock_add,
            patch.object(
                loop, "remove_signal_handler", new_callable=MagicMock
            ) as mock_remove,
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "shutdown", new_callable=AsyncMock),
            patch.object(util_mod.Util, "check_pid_file", return_value=None),
            patch.object(util_mod.Util, "write_pid_file"),
            patch.object(util_mod.Util, "remove_pid_file"),
        ):
            async with main.lifespan(app):
                mock_add.assert_called_once()
                # Handler is registered for SIGHUP pointing at on_sighup.
                registered_signal = mock_add.call_args.args[0]
                assert registered_signal == signal.SIGHUP
            mock_remove.assert_called_once_with(signal.SIGHUP)


# --- Static files ---


class TestSetupStaticFiles:
    async def test_mounts_static_files_and_adds_middleware(self, tmp_path):
        # Create a fake frontend directory with an index.html
        (tmp_path / "index.html").write_text("<html>hello</html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"hello" in resp.content

    async def test_no_cache_headers_on_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>hi</html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    async def test_no_cache_headers_on_js(self, tmp_path):
        (tmp_path / "app.js").write_text("console.log('hi')")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/app.js")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )

    async def test_no_cache_headers_not_on_other_files(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/image.png")
        assert "Cache-Control" not in resp.headers

    async def test_mounts_branding_from_data_dir(self, tmp_path):
        # When <data_dir>/branding exists, it is served at /branding.
        (tmp_path / "index.html").write_text("<html></html>")
        branding = tmp_path / "branding" / "logo.png"
        branding.parent.mkdir(parents=True, exist_ok=True)
        branding.write_bytes(b"\x89PNG\r\n\x1a\n")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/logo.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

    async def test_branding_prefers_customize_dir(self, tmp_path, monkeypatch):
        # When <KLANGK_CUSTOMIZE_DIR>/branding exists, it is preferred
        # over <data_dir>/branding.  See #1360.
        (tmp_path / "index.html").write_text("<html></html>")
        custom = tmp_path / "cust"
        branding = custom / "branding"
        branding.mkdir(parents=True)

        test_app = FastAPI()
        _settings = make_settings({"KLANGK_CUSTOMIZE_DIR": str(custom)})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        branding_route = [
            r for r in test_app.routes if getattr(r, "path", "") == "/branding"
        ]
        assert branding_route
        assert Path(branding_route[0].app.directory) == branding

    async def test_branding_skipped_when_no_dir_exists(self, tmp_path):
        # When neither customize_dir/branding nor data_dir/branding
        # exists, the /branding mount is skipped entirely.
        (tmp_path / "index.html").write_text("<html></html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        branding_route = [
            r for r in test_app.routes if getattr(r, "path", "") == "/branding"
        ]
        assert not branding_route

    async def test_branding_mount_404_for_missing_file(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "branding").mkdir()
        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/nope.png")
        assert resp.status_code == 404

    async def test_branding_files_get_no_no_cache_header(self, tmp_path):
        # Logos should be cacheable; the no-cache middleware only targets
        # .html/.js/"/", not branding assets.
        (tmp_path / "index.html").write_text("<html></html>")
        branding = tmp_path / "branding" / "logo.png"
        branding.parent.mkdir(parents=True, exist_ok=True)
        branding.write_bytes(b"\x89PNG")
        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/logo.png")
        assert resp.status_code == 200
        assert "Cache-Control" not in resp.headers


# --- Logfire ---


class TestSetupLogfire:
    def test_no_token_returns_false(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        app = FastAPI()
        assert main.setup_logfire(app) is False

    def test_with_token_instruments_app(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.delenv("LOGFIRE_BASE_URL", raising=False)
        monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            result = main.setup_logfire(app)
        assert result is True
        mock_logfire.configure.assert_called_once_with()
        mock_logfire.instrument_fastapi.assert_called_once_with(app)

    def test_base_url_passed_via_advanced_options(self, monkeypatch):
        # LOGFIRE_BASE_URL must be passed as advanced=AdvancedOptions(base_url=...),
        # not as the deprecated top-level base_url= argument (#1410).
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://logfire.example.com")
        monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            result = main.setup_logfire(app)
        assert result is True
        mock_logfire.AdvancedOptions.assert_called_once_with(
            base_url="https://logfire.example.com"
        )
        mock_logfire.configure.assert_called_once()
        configure_kwargs = mock_logfire.configure.call_args.kwargs
        assert "advanced" in configure_kwargs
        assert (
            configure_kwargs["advanced"]
            is mock_logfire.AdvancedOptions.return_value
        )
        assert "base_url" not in configure_kwargs


class TestCorsOrigins:
    """Moved to test_util.py (Util.cors_origins, #1503)."""

    def test_with_base_url_and_environment(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://custom.logfire")
        monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            main.setup_logfire(app)
        mock_logfire.AdvancedOptions.assert_called_once_with(
            base_url="https://custom.logfire"
        )
        mock_logfire.configure.assert_called_once_with(
            advanced=mock_logfire.AdvancedOptions.return_value,
            environment="staging",
        )


# --- PID file helpers ---


class TestPidFile:
    """PID-file helpers are methods of ``Util`` (``app.state.util``) since
    #1565 — the PID file is part of instance identity, so read/write/check
    live on the same ``Util`` that owns the ID."""

    def _util_with_pid_file(self, monkeypatch, pid_file):
        """A Util whose ``pid_file_path()`` returns ``pid_file``.

        ``instance_id()`` is short-circuited so it never touches the
        filesystem (the real path would read ``<data_dir>/instance-id``)."""
        util = util_mod.Util(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        monkeypatch.setattr(util, "_instance_id", "iid")
        monkeypatch.setattr(util, "pid_file_path", lambda: pid_file)
        return util

    def test_check_pid_file_no_file(self, tmp_path, monkeypatch):
        util = self._util_with_pid_file(
            monkeypatch, tmp_path / "klangk-test.pid"
        )
        assert util.check_pid_file() is None

    def test_check_pid_file_stale_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID that (almost certainly) doesn't exist
        pid_file.write_text("2000000")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None
        assert not pid_file.exists()

    def test_check_pid_file_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text(str(os.getpid()))
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None

    def test_check_pid_file_live_pid_permission_error(
        self, tmp_path, monkeypatch
    ):
        pid_file = tmp_path / "klangk-test.pid"
        # PID 1 (init) is always alive
        pid_file.write_text("1")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        # os.kill(1, 0) raises PermissionError for non-root
        result = util.check_pid_file()
        assert result == 1

    def test_check_pid_file_live_foreign_pid(self, tmp_path, monkeypatch):
        """Live PID that os.kill(pid, 0) succeeds on (not our PID)."""
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID we know is alive and can signal (our parent process)
        ppid = os.getppid()
        pid_file.write_text(str(ppid))
        util = self._util_with_pid_file(monkeypatch, pid_file)
        result = util.check_pid_file()
        assert result == ppid

    def test_check_pid_file_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("not-a-number")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None

    def test_write_and_remove_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        util = self._util_with_pid_file(monkeypatch, pid_file)
        util.write_pid_file()
        assert pid_file.read_text() == str(os.getpid())
        util.remove_pid_file()
        assert not pid_file.exists()

    def test_remove_pid_file_only_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("99999")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        util.remove_pid_file()
        # File should still exist — not our PID
        assert pid_file.exists()

    def test_remove_pid_file_missing(self, tmp_path, monkeypatch):
        util = self._util_with_pid_file(
            monkeypatch, tmp_path / "klangk-test.pid"
        )
        # Should not raise
        util.remove_pid_file()

    async def test_pid_file_path_uses_state_dir(self, tmp_path):
        """pid_file_path() lives in state_dir and embeds the instance ID."""
        util = util_mod.Util(
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    settings=make_settings({"KLANGK_STATE_DIR": str(tmp_path)})
                )
            )
        )
        monkeypatch_id = "12345678-1234-1234-1234-123456789abc"
        util._instance_id = monkeypatch_id
        path = util.pid_file_path()
        assert path.parent == tmp_path
        assert path.name == f"klangk-{monkeypatch_id}.pid"


class TestBuildApp:
    """Tests for build_app() composition root (#1426)."""

    def test_build_app_returns_fastapi(self):
        settings = make_settings({})
        app = main.build_app(settings)
        assert isinstance(app, FastAPI)

    def test_build_app_sets_state_settings(self):
        settings = make_settings({})
        app = main.build_app(settings)
        assert app.state.settings is settings

    def test_build_app_includes_routers(self):
        app = main.build_app(make_settings({}))
        paths = set(app.openapi()["paths"].keys())
        assert "/api/v1/config" in paths  # api router with prefix

    def test_build_app_has_ws_endpoint(self):
        app = main.build_app(make_settings({}))
        ws_paths = {
            r.path
            for r in app.routes
            if hasattr(r, "path") and r.path == "/ws"
        }
        assert "/ws" in ws_paths

    def test_build_app_registers_exception_handlers(self):
        app = main.build_app(make_settings({}))
        assert model.AgentPrincipalError in app.exception_handlers

    def test_no_module_level_app_attribute(self):
        """main.py no longer exposes an ``app`` attribute (#1454).

        The composition root is sealed: ``klangkd`` builds the app explicitly.
        E2E suites launch ``e2e-tests/runtestserver.py`` (which builds the
        app and passes the object to uvicorn) instead of a string import.
        """
        assert not hasattr(main, "app")

    def test_runtestserver_builds_app(self):
        """The E2E launcher script builds a real FastAPI app."""
        import runpy
        from pathlib import Path

        script = (
            Path(__file__).resolve().parent.parent
            / "e2e-tests"
            / "runtestserver.py"
        )
        # Don't execute __main__ (that would call uvicorn.run); just verify
        # the module imports and exposes build_app correctly.
        ns = runpy.run_path(str(script), run_name="not_main")
        app = ns["build_app"](ns["KlangkSettings"](os.environ))
        assert isinstance(app, FastAPI)


class TestGetAppDep:
    """Tests for get_app_dep per-request bridge (#1426)."""

    def test_returns_app(self, app_state):
        settings = make_settings({})
        app = main.build_app(settings)
        request = MagicMock()
        request.app = app
        result = main.get_app_dep(request)
        assert result is app
        assert result.state.settings is settings


class TestLiveCORSMiddleware:
    """LiveCORSMiddleware reads origins from app state on each request (#1610)."""

    async def test_rebuilds_on_settings_change(self):
        settings1 = make_settings({"KLANGK_CORS_ORIGINS": "http://a.example"})
        app = main.build_app(settings1)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.options(
                "/api/v1/version",
                headers={
                    "Origin": "http://a.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert (
                resp.headers.get("access-control-allow-origin")
                == "http://a.example"
            )

            # Swap settings with a different origin
            settings2 = make_settings(
                {"KLANGK_CORS_ORIGINS": "http://b.example"}
            )
            app.state.settings = settings2

            resp2 = await client.options(
                "/api/v1/version",
                headers={
                    "Origin": "http://b.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert (
                resp2.headers.get("access-control-allow-origin")
                == "http://b.example"
            )

    async def test_caches_until_settings_change(self):
        import types as _types

        settings = make_settings({"KLANGK_CORS_ORIGINS": "http://x.example"})
        app = _types.SimpleNamespace(
            state=_types.SimpleNamespace(
                settings=settings,
                util=util_mod.Util(
                    _types.SimpleNamespace(
                        state=_types.SimpleNamespace(settings=settings)
                    )
                ),
            )
        )
        live_cors = main.LiveCORSMiddleware(
            lambda scope, recv, send: None, fastapi_app=app
        )
        # First call builds the inner middleware
        inner1 = live_cors._rebuild_if_needed()
        inner2 = live_cors._rebuild_if_needed()
        assert inner1 is inner2  # cached — same settings object

        # Swap settings → inner changes
        settings2 = make_settings({"KLANGK_CORS_ORIGINS": "http://y.example"})
        app.state.settings = settings2
        app.state.util = util_mod.Util(
            _types.SimpleNamespace(
                state=_types.SimpleNamespace(settings=settings2)
            )
        )
        inner3 = live_cors._rebuild_if_needed()
        assert inner3 is not inner1


class TestRemountFrontend:
    """Lifecycle._remount_frontend replaces the frontend mount (#1610)."""

    async def test_remount_swaps_static_dir(self, tmp_path, app_state):
        lc = main.Lifecycle(app_state)
        app = FastAPI()
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        (old_dir / "index.html").write_text("old")
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "index.html").write_text("new")
        app.state.settings = make_settings({})
        app.state.util = util_mod.Util(app)
        main.setup_static_files(app, old_dir)

        new_settings = make_settings({"KLANGK_FRONTEND_DIR": str(new_dir)})
        lc._remount_frontend(app, new_settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"new" in resp.content

    async def test_remount_removes_mount_when_dir_missing(
        self, tmp_path, app_state
    ):
        lc = main.Lifecycle(app_state)
        app = FastAPI()
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        (old_dir / "index.html").write_text("old")
        app.state.settings = make_settings({})
        app.state.util = util_mod.Util(app)
        main.setup_static_files(app, old_dir)

        new_settings = make_settings(
            {"KLANGK_FRONTEND_DIR": str(tmp_path / "nonexistent")}
        )
        lc._remount_frontend(app, new_settings)

        # The old mount should be gone — no routes named "frontend"
        frontend_routes = [
            r for r in app.routes if getattr(r, "name", None) == "frontend"
        ]
        assert frontend_routes == []

    async def test_apply_reloaded_settings_remounts_frontend(
        self, db, tmp_path
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old_settings = app_state.state.settings
        new_settings = make_settings(
            dict(old_settings._reload_env, KLANGK_FRONTEND_DIR=str(tmp_path))
        )
        with patch.object(lc, "_remount_frontend") as mock_remount:
            await lc._apply_reloaded_settings(new_settings)
        mock_remount.assert_called_once()

    async def test_apply_reloaded_settings_skips_remount_when_unchanged(
        self, db
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old_settings = app_state.state.settings
        # Same frontend_dir → no remount
        new_settings = make_settings(dict(old_settings._reload_env))
        with patch.object(lc, "_remount_frontend") as mock_remount:
            await lc._apply_reloaded_settings(new_settings)
        mock_remount.assert_not_called()
