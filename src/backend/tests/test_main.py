"""Tests for main.py: lifespan, seed user, static files, logfire."""

import asyncio
import os
import signal
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk_backend import main, model


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("KLANGK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        user = await model.get_user_by_email("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("KLANGK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        # Call again — should not raise
        await main.seed_default_user()
        user = await model.get_user_by_email("seed-test")
        assert user is not None

    async def test_generates_password_when_not_set(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "gen-test")
        monkeypatch.delenv("KLANGK_DEFAULT_PASSWORD", raising=False)
        await main.seed_default_user()
        user = await model.get_user_by_email("gen-test")
        assert user is not None
        # User exists and is in the admin group
        admin_group = await model.get_group_by_name("admin")
        assert admin_group is not None
        group_ids = await model.get_user_group_ids(user["id"])
        assert admin_group["id"] in group_ids

    async def test_generated_password_printed_to_stderr(
        self, db, monkeypatch, caplog, capsys
    ):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "log-test")
        monkeypatch.delenv("KLANGK_DEFAULT_PASSWORD", raising=False)
        import logging

        with caplog.at_level(logging.INFO):
            await main.seed_default_user()
        # Password must NOT appear in log output (security)
        assert "password printed to stderr" in caplog.text
        assert "log-test" in caplog.text
        # Password appears on stderr only
        captured = capsys.readouterr()
        assert "Default admin password for" in captured.err


# --- no-auth bind safety gate (#1374) ---


class TestNoAuthBindSafety:
    """enforce_no_auth_bind_safety() — refuse none mode on a non-loopback
    bind unless explicitly overridden."""

    def test_noop_when_not_none_mode(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "password")
        monkeypatch.setenv("KLANGK_LISTEN", "0.0.0.0")
        # Returns None, raises nothing.
        assert main.enforce_no_auth_bind_safety() is None

    def test_allows_loopback_ipv4(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        monkeypatch.setenv("KLANGK_LISTEN", "127.0.0.1")
        assert main.enforce_no_auth_bind_safety() is None

    def test_allows_loopback_ipv6_and_localhost(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        for host in ("::1", "localhost"):
            monkeypatch.setenv("KLANGK_LISTEN", host)
            assert main.enforce_no_auth_bind_safety() is None

    def test_allows_loopback_default_when_listen_unset(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        monkeypatch.delenv("KLANGK_LISTEN", raising=False)
        # KLANGK_LISTEN defaults to 127.0.0.1 (#1375).
        assert main.enforce_no_auth_bind_safety() is None

    def test_refuses_non_loopback_bind(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        monkeypatch.setenv("KLANGK_LISTEN", "0.0.0.0")
        with pytest.raises(SystemExit) as exc_info:
            main.enforce_no_auth_bind_safety()
        msg = str(exc_info.value)
        assert "KLANGK_AUTH_MODES=none" in msg
        assert "loopback" in msg
        assert "KLANGK_ALLOW_INSECURE_NO_AUTH=1" in msg
        assert "0.0.0.0" in msg

    def test_override_flag_allows_non_loopback(self, monkeypatch, caplog):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        monkeypatch.setenv("KLANGK_LISTEN", "0.0.0.0")
        monkeypatch.setenv("KLANGK_ALLOW_INSECURE_NO_AUTH", "1")
        import logging

        with caplog.at_level(logging.WARNING):
            assert main.enforce_no_auth_bind_safety() is None
        assert "non-loopback bind" in caplog.text


# --- Seed agent user ---


class TestSeedAgentUser:
    async def test_creates_agent_user(self, db):
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user is not None
        assert user["email"] == "clanker@example.com"
        assert user["handle"] == "clanker"

    async def test_custom_env_vars(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_AGENT_EMAIL", "bot@test.com")
        monkeypatch.setenv("KLANGK_AGENT_HANDLE", "TestBot")
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user is not None
        assert user["email"] == "bot@test.com"
        assert user["handle"] == "TestBot"

    async def test_upserts_existing(self, db, monkeypatch):
        await main.seed_agent_user()
        monkeypatch.setenv("KLANGK_AGENT_EMAIL", "new@test.com")
        monkeypatch.setenv("KLANGK_AGENT_HANDLE", "NewBot")
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user["email"] == "new@test.com"
        assert user["handle"] == "NewBot"

    async def test_clears_cache(self, db):
        # Prime cache with fallback
        await model.get_agent_user()
        await main.seed_agent_user()
        # Cache should now reflect DB values
        agent = await model.get_agent_user()
        assert agent["email"] == "clanker@example.com"

    async def test_users_handle_has_unique_constraint(self, db):
        """The users.handle UNIQUE constraint is the structural backstop.

        Confirms a duplicate handle raises IntegrityError at the DB layer,
        independent of seed_agent_user's pre-check.  See #1137.
        """
        async with model.transaction() as db_conn:
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
        self, db, monkeypatch
    ):
        """Seeding the agent with a live user's handle fails cleanly.

        The destructive path is ensure_home_symlink migrating that user's
        files into the agent's tree; the guard must abort before any such
        work.  See #1137.
        """
        human = await model.create_user(
            "alice@example.com", "hash", verified=True
        )
        assert human["handle"] == "alice"
        monkeypatch.setenv("KLANGK_AGENT_HANDLE", "alice")
        with pytest.raises(RuntimeError, match="alice"):
            await main.seed_agent_user()
        # Human user is untouched.
        refreshed = await model.get_user_by_id(human["id"])
        assert refreshed["handle"] == "alice"
        # Agent was not created with the colliding handle.
        assert await model.get_user_by_id(model.AGENT_USER_ID) is None

    async def test_seed_rename_to_human_handle_refuses(self, db, monkeypatch):
        """Re-seeding the agent onto a human's handle fails, leaves agent as-is."""
        await main.seed_agent_user()  # agent handle = clanker
        human = await model.create_user(
            "alice@example.com", "hash", verified=True
        )
        monkeypatch.setenv("KLANGK_AGENT_HANDLE", "alice")
        with pytest.raises(RuntimeError, match="already used by another user"):
            await main.seed_agent_user()
        # Agent keeps its original handle; human untouched.
        agent = await model.get_user_by_id(model.AGENT_USER_ID)
        assert agent["handle"] == "clanker"
        refreshed = await model.get_user_by_id(human["id"])
        assert refreshed["handle"] == "alice"

    async def test_collision_leaves_human_files_untouched(
        self, db, monkeypatch, tmp_path
    ):
        """A handle collision never reaches ensure_home_symlink's adoption.

        Builds the on-disk layout that the destructive branch would migrate
        (a /home/<handle> symlink -> .users/<human-uid> with files) and
        confirms a colliding agent seed aborts before any file moves.  See
        #1137.
        """
        human = await model.create_user(
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

        monkeypatch.setenv("KLANGK_AGENT_HANDLE", "alice")
        with pytest.raises(RuntimeError):
            await main.seed_agent_user()

        # Human's files are exactly where they were — nothing migrated.
        assert (human_dir / "secret.txt").read_text() == "alice's secrets"
        assert os.readlink(home / "alice") == f".users/{human['id']}"
        # No agent user directory was created.
        assert not (users_dir / model.AGENT_USER_ID).exists()


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        app = FastAPI()
        with (
            patch.object(
                main.container.registry,
                "adopt_orphaned_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                main.container.registry, "start_cleanup_loop"
            ) as mock_start,
            patch.object(
                main.container.registry,
                "shutdown",
                new_callable=AsyncMock,
            ) as mock_shutdown,
            patch(
                "klangk_backend.main.check_pid_file", return_value=None
            ) as mock_check,
            patch("klangk_backend.main.write_pid_file") as mock_write,
            patch("klangk_backend.main.remove_pid_file") as mock_remove,
        ):
            async with main.lifespan(app):
                mock_check.assert_called_once()
                mock_write.assert_called_once()
                mock_adopt.assert_awaited_once()
                mock_start.assert_called_once()
            mock_shutdown.assert_awaited_once()
            mock_remove.assert_called_once()

    async def test_lifespan_refuses_if_pid_alive(self, db, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        app = FastAPI()
        with (
            patch("klangk_backend.main.check_pid_file", return_value=12345),
            pytest.raises(SystemExit),
        ):
            async with main.lifespan(app):
                pass  # pragma: no cover


# --- SIGHUP runtime restart (#1212) ---


class TestStartupShutdownRestart:
    @pytest.fixture(autouse=True)
    def _reset_restart_lock(self):
        # ``main._restart_lock`` is a module-global lazily created on first
        # use. Without a reset, lock state leaks across tests, making them
        # order-dependent (and breaking some tests in isolation or under
        # pytest-randomly's shuffle). Reset to the pre-first-use floor before
        # every test in this class so each one is self-contained (#1242).
        main._restart_lock = None
        yield
        main._restart_lock = None

    async def test_startup_calls_container_sequence(self):
        with (
            patch.object(
                main.container.registry,
                "prewarm_podman",
                new_callable=AsyncMock,
            ) as mock_prewarm,
            patch.object(
                main.container.registry,
                "adopt_orphaned_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                main.container.registry, "start_cleanup_loop"
            ) as mock_cleanup,
            patch.object(
                main.container.registry, "start_health_loop"
            ) as mock_health,
            patch(
                "klangk_backend.main.workspaces.auto_start_workspaces",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_autostart,
        ):
            await main.startup()
        mock_prewarm.assert_awaited_once()
        mock_adopt.assert_awaited_once()
        mock_cleanup.assert_called_once()
        mock_health.assert_called_once()
        mock_autostart.assert_awaited_once()

    async def test_runtime_shutdown_tears_down_layers(self):
        with (
            patch(
                "klangk_backend.main.wshandler.disconnect_all_websockets",
                new_callable=AsyncMock,
            ) as mock_disc,
            patch(
                "klangk_backend.main.agent.stop_all_sessions",
                new_callable=AsyncMock,
            ) as mock_stop_agents,
            patch(
                "klangk_backend.main.wshandler.clear_agent_mention_state"
            ) as mock_clear,
            patch.object(
                main.container.registry, "shutdown", new_callable=AsyncMock
            ) as mock_shutdown,
        ):
            await main.runtime_shutdown()
        mock_disc.assert_awaited_once()
        mock_stop_agents.assert_awaited_once()
        mock_clear.assert_called_once()
        mock_shutdown.assert_awaited_once()

    async def test_process_shutdown_disposes(self):
        with (
            patch("klangk_backend.main.remove_pid_file") as mock_remove,
            patch(
                "klangk_backend.main.model.dispose_engine",
                new_callable=AsyncMock,
            ) as mock_dispose,
        ):
            await main.process_shutdown()
        mock_remove.assert_called_once()
        mock_dispose.assert_awaited_once()

    async def test_restart_runtime_runs_shutdown_then_startup(self):
        main._restart_lock = None  # force fresh lock creation
        with (
            patch(
                "klangk_backend.main.runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch(
                "klangk_backend.main.startup", new_callable=AsyncMock
            ) as mock_up,
        ):
            await main.restart_runtime()
        mock_down.assert_awaited_once()
        mock_up.assert_awaited_once()
        # Lock was created and is now held-free.
        assert main._restart_lock is not None

    async def test_restart_runtime_reuses_existing_lock(self):
        # Seed a lock explicitly; ``restart_runtime`` must reuse it rather
        # than create a new one. (Previously this relied on a prior test's
        # side effect of populating the module-global, which made it
        # order-dependent and broken in isolation — #1242.)
        main._restart_lock = asyncio.Lock()
        existing = main._restart_lock
        with (
            patch(
                "klangk_backend.main.runtime_shutdown", new_callable=AsyncMock
            ),
            patch("klangk_backend.main.startup", new_callable=AsyncMock),
        ):
            await main.restart_runtime()
        # Same lock object reused, not replaced.
        assert main._restart_lock is existing

    async def test_restart_lock_serializes_concurrent_calls(self):
        """Two restarts kicked off together run strictly one-after-another."""
        main._restart_lock = None
        order = []

        async def fake_shutdown():
            order.append("down-start")
            await asyncio.sleep(0.01)
            order.append("down-end")

        async def fake_startup():
            order.append("up")

        with (
            patch(
                "klangk_backend.main.runtime_shutdown",
                side_effect=fake_shutdown,
            ),
            patch("klangk_backend.main.startup", side_effect=fake_startup),
        ):
            await asyncio.gather(
                main.restart_runtime(), main.restart_runtime()
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

    async def test_on_sighup_schedules_restart(self):
        """on_sighup creates a task that runs restart_runtime."""
        with patch(
            "klangk_backend.main.restart_runtime", new_callable=AsyncMock
        ) as mock_restart:
            main.on_sighup()
            # Let the scheduled task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        mock_restart.assert_awaited_once()

    async def test_lifespan_registers_sighup_handler(self, db, monkeypatch):
        """The lifespan installs (and removes) a SIGHUP handler."""
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        app = FastAPI()
        loop = asyncio.get_running_loop()
        with (
            patch.object(
                loop, "add_signal_handler", new_callable=MagicMock
            ) as mock_add,
            patch.object(
                loop, "remove_signal_handler", new_callable=MagicMock
            ) as mock_remove,
            patch.object(
                main.container.registry,
                "adopt_orphaned_containers",
                new_callable=AsyncMock,
            ),
            patch.object(main.container.registry, "start_cleanup_loop"),
            patch.object(
                main.container.registry, "shutdown", new_callable=AsyncMock
            ),
            patch("klangk_backend.main.check_pid_file", return_value=None),
            patch("klangk_backend.main.write_pid_file"),
            patch("klangk_backend.main.remove_pid_file"),
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
        monkeypatch.setenv("KLANGK_CUSTOMIZE_DIR", str(custom))

        test_app = FastAPI()
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
        main.setup_static_files(test_app, tmp_path)

        branding_route = [
            r for r in test_app.routes if getattr(r, "path", "") == "/branding"
        ]
        assert not branding_route

    async def test_branding_mount_404_for_missing_file(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "branding").mkdir()
        test_app = FastAPI()
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


class TestCorsOrigins:
    def test_default_localhost(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        # No override -> bare localhost; the port is NOT synthesized from
        # KLANGK_NGINX_PORT (consistent with hosted-app URL construction).
        assert main.cors_origins() == ["http://localhost"]

    def test_nginx_port_not_synthesized(self, monkeypatch):
        """KLANGK_NGINX_PORT does not leak into the CORS origin.

        It is internal container wiring, not the browser origin; the port
        must come from KLANGK_HOSTING_HOSTNAME. Setting the nginx port
        therefore has no effect on the derived origin (#1240).
        """
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "9000")
        assert main.cors_origins() == ["http://localhost"]

    def test_hosting_hostname_carries_port(self, monkeypatch):
        """The port lives in KLANGK_HOSTING_HOSTNAME, not a separate var."""
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "localhost:8996")
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        assert main.cors_origins() == ["http://localhost:8996"]

    def test_hosting_hostname(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "klangk.example.com")
        monkeypatch.setenv("KLANGK_HOSTING_PROTO", "https")
        assert main.cors_origins() == ["https://klangk.example.com"]

    def test_hosting_hostname_default_proto(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "klangk.example.com")
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        assert main.cors_origins() == ["http://klangk.example.com"]

    def test_explicit_origins(self, monkeypatch):
        monkeypatch.setenv(
            "KLANGK_CORS_ORIGINS",
            "https://a.example.com, https://b.example.com",
        )
        assert main.cors_origins() == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_explicit_origins_strips_empties(self, monkeypatch):
        monkeypatch.setenv("KLANGK_CORS_ORIGINS", "https://a.com,,")
        assert main.cors_origins() == ["https://a.com"]

    def test_explicit_overrides_hosting(self, monkeypatch):
        monkeypatch.setenv("KLANGK_CORS_ORIGINS", "https://override.com")
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "ignored.com")
        assert main.cors_origins() == ["https://override.com"]

    def test_with_base_url_and_environment(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://custom.logfire")
        monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            main.setup_logfire(app)
        mock_logfire.configure.assert_called_once_with(
            base_url="https://custom.logfire",
            environment="staging",
        )


# --- PID file helpers ---


class TestPidFile:
    def test_check_pid_file_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            main, "pid_file_path", lambda: tmp_path / "klangk-test.pid"
        )
        assert main.check_pid_file() is None

    def test_check_pid_file_stale_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID that (almost certainly) doesn't exist
        pid_file.write_text("2000000")
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None
        assert not pid_file.exists()

    def test_check_pid_file_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None

    def test_check_pid_file_live_pid_permission_error(
        self, tmp_path, monkeypatch
    ):
        pid_file = tmp_path / "klangk-test.pid"
        # PID 1 (init) is always alive
        pid_file.write_text("1")
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        # os.kill(1, 0) raises PermissionError for non-root
        result = main.check_pid_file()
        assert result == 1

    def test_check_pid_file_live_foreign_pid(self, tmp_path, monkeypatch):
        """Live PID that os.kill(pid, 0) succeeds on (not our PID)."""
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID we know is alive and can signal (our parent process)
        ppid = os.getppid()
        pid_file.write_text(str(ppid))
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        result = main.check_pid_file()
        assert result == ppid

    def test_check_pid_file_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None

    def test_write_and_remove_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        main.write_pid_file()
        assert pid_file.read_text() == str(os.getpid())
        main.remove_pid_file()
        assert not pid_file.exists()

    def test_remove_pid_file_only_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        main.remove_pid_file()
        # File should still exist — not our PID
        assert pid_file.exists()

    def test_remove_pid_file_missing(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        monkeypatch.setattr(main, "pid_file_path", lambda: pid_file)
        # Should not raise
        main.remove_pid_file()

    async def test_pid_file_path_uses_runtime_dir(self, db):
        path = main.pid_file_path()
        assert path.parent == main.runtime_dir()
        iid = main.model.get_instance_id()
        assert path.name == f"klangk-{iid}.pid"

    def test_runtime_dir_prefers_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert main.runtime_dir() == tmp_path

    def test_runtime_dir_linux_run_user(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        linux_run = Path(f"/run/user/{os.getuid()}")
        if linux_run.is_dir():
            assert main.runtime_dir() == linux_run

    def test_runtime_dir_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        # Make /run/user/<uid> appear non-existent
        orig_is_dir = Path.is_dir

        def fake_is_dir(self):
            if str(self).startswith("/run/user/"):
                return False
            return orig_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", fake_is_dir)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        result = main.runtime_dir()
        assert result == tmp_path / ".klangk" / "run"
        assert result.exists()
