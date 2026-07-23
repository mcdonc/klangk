"""Shared fixtures for backend unit tests."""

import os

# Must be set before coverage.py initialises in each xdist worker so that
# code executed inside SQLAlchemy's greenlet context is tracked.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

import types

import bcrypt

import pytest

from klangk.settings import KlangkSettings

from _helpers import make_settings

# Use fast bcrypt rounds (4 instead of default 12) for all tests.
_original_gensalt = bcrypt.gensalt


def _fast_gensalt(rounds=4, prefix=b"2b"):
    return _original_gensalt(rounds=4, prefix=prefix)


bcrypt.gensalt = _fast_gensalt

_TEST_PASSWORD = "testpass"
_TEST_PASSWORD_HASH = bcrypt.hashpw(
    _TEST_PASSWORD.encode(), bcrypt.gensalt()
).decode()


@pytest.fixture(autouse=True)
def _disable_proxy(monkeypatch):
    """Suppress proxy spawn across the suite.

    The lifespan's proxy watchdog is unconditional in real runs; tests boot
    the app (often via the lifespan) and never want a real proxy (nginx)
    process.
    Sets the internal, non-user-facing ``_KLANGKD_DISABLE_PROXY`` kill switch.
    """
    monkeypatch.setenv("_KLANGKD_DISABLE_PROXY", "1")


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point KLANGKD_DATA_DIR / KLANGKD_STATE_DIR / KLANGKD_CUSTOMIZE_DIR at temp dirs per test."""
    monkeypatch.setenv("KLANGKD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KLANGKD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KLANGKD_CUSTOMIZE_DIR", str(tmp_path / "customize"))
    monkeypatch.delenv("KLANGKD_IMAGE_PULL_POLICY", raising=False)
    # Build the per-test DB from the new env so the engine cache and db_path
    # point at the per-test temp dir (#1452: no import-time globals). Stash it
    # in the per-test holder so the ``app_state`` fixture (and any
    # ``wire_db_and_model`` caller) reuses this exact schema-bearing instance —
    # not a fresh DB on a different temp path (which would hit "no such
    # table"). This replaces the pre-#1578 ``_current_db`` ContextVar bind
    # (#1578); its env-only lazy fallback was the #1551 divergence path.
    from klangk.model.db import DB
    from _helpers import set_test_db, reset_test_db

    set_test_db(
        DB(
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    settings=KlangkSettings(os.environ)
                )
            )
        )
    )
    # Clear agent caches so each test starts fresh.
    from klangk.model import clear_agent_cache

    clear_agent_cache()
    yield tmp_path
    reset_test_db()


@pytest.fixture
async def db(app_state):
    """Initialize the schema and return the per-test DB."""
    await app_state.state.model.init_db()
    return app_state.state.db


@pytest.fixture
async def agent_user(app_state):
    """Seed the chat agent user into the DB."""
    from klangk.model import AGENT_USER_ID

    await app_state.state.model.init_db()
    async with app_state.state.db.transaction() as agent_db:
        await agent_db.execute(
            "INSERT OR REPLACE INTO users"
            " (id, email, password_hash, verified, provider, handle)"
            " VALUES (?, ?, NULL, 1, 'system', ?)",
            (AGENT_USER_ID, "clanker@example.com", "clanker"),
        )
    app_state.state.model.users.clear_agent_cache()


@pytest.fixture
async def user(app_state):
    """Create a test user and return it."""
    await app_state.state.model.init_db()
    return await app_state.state.model.users.create_user(
        "testuser@example.com", _TEST_PASSWORD_HASH, verified=True
    )


@pytest.fixture
async def admin_group(app_state):
    """Create the admin group and seed default ACLs."""
    from klangk.model import (
        ACTION_ALLOW,
        ACTION_DENY,
        PRINCIPAL_GROUP,
        PRINCIPAL_SYSTEM,
        SYSTEM_AUTHENTICATED,
        SYSTEM_EVERYONE,
    )

    await app_state.state.model.init_db()
    group = await app_state.state.model.users.create_group(
        "admin", description="Administrators"
    )
    acl = app_state.state.model.acl
    # Seed default ACLs
    await acl.add_acl_entry(
        "/",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await acl.add_acl_entry(
        "/",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    await acl.add_acl_entry(
        "/workspaces",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await acl.add_acl_entry(
        "/admin",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_GROUP,
        group_id=group["id"],
    )
    await acl.add_acl_entry(
        "/admin",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    return group


@pytest.fixture
async def admin_user(admin_group, app_state):
    """Create a test user in the admin group and return it."""
    user = await app_state.state.model.users.create_user(
        "testadmin@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    await app_state.state.model.users.add_user_to_group(
        user["id"], admin_group["id"]
    )
    return user


@pytest.fixture
async def app_state(temp_data_dir):
    """Build a minimal mock app with owned instances wired (#1484).

    Returns a mock ``app`` object whose ``.state`` holds the subsystem
    instances, matching the real ``build_app()`` shape.  Shared across all
    test files — each test file that needs app_state requests it rather
    than defining its own.  Built fresh per test from the temp_data_dir's
    settings so every owned instance points at the per-test tmp dir.

    Despite the fixture name (kept for backwards compatibility with the
    ~3000 existing call sites), this returns an object with a ``.state``
    attribute — the same shape subsystem constructors receive as ``app``.
    """
    import types

    from klangk.auth import Auth
    from klangk.container import ContainerRegistry
    from klangk.emailsvc import EmailService
    from klangk.util import Util
    from klangk.workspaces import Workspaces

    settings = make_settings(
        {
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DATA_DIR": str(temp_data_dir),
            "KLANGKD_STATE_DIR": str(temp_data_dir / "state"),
            "KLANGKD_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    state = types.SimpleNamespace(settings=settings)
    app = types.SimpleNamespace(state=state)
    state.auth = Auth(app)
    registry = ContainerRegistry(app)
    state.container_registry = registry
    state.workspaces = Workspaces(app)
    state.email = EmailService(app)
    state.util = Util(app)
    # Wire DB + Model so every domain reached via
    # app.state.model.* resolves the per-test DB (#1578). With the
    # _current_db ContextVar gone, app.state.db is the single owner.
    from _helpers import wire_db_and_model

    wire_db_and_model(app)
    # NOTE: schema is NOT initialized here. ``db`` and the seed fixtures
    # (``user``/``agent_user``/``admin_group``) call ``init_db`` themselves
    # so that migration tests can request ``app_state``, plant a legacy
    # schema via aiosqlite, and only then run ``app.state.model.init_db()``
    # (#1578).
    # Resolve the instance ID into the per-test util (writes
    # <data_dir>/instance-id), so consumers of app.state.util.instance_id()
    # get a real value without each test calling resolve explicitly.
    state.util.resolve_instance_id()
    return app


@pytest.fixture
async def workspace(user, app_state):
    """Create a test workspace (without port allocation)."""
    workspace = await app_state.state.model.workspaces.create_workspace(
        user["id"], "test-workspace"
    )
    return workspace
