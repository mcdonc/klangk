"""Shared fixtures for backend unit tests."""

import os

# Must be set before coverage.py initialises in each xdist worker so that
# code executed inside SQLAlchemy's greenlet context is tracked.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

import bcrypt

import pytest

from klangk_backend.settings import KlangkSettings

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
def _disable_nginx(monkeypatch):
    """Suppress nginx spawn across the suite.

    The lifespan's nginx watchdog is unconditional in real runs; tests boot
    the app (often via the lifespan) and never want a real nginx process.
    Sets the internal, non-user-facing ``_KLANGK_DISABLE_NGINX`` kill switch.
    """
    monkeypatch.setenv("_KLANGK_DISABLE_NGINX", "1")


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point KLANGK_DATA_DIR / KLANGK_STATE_DIR / KLANGK_CUSTOMIZE_DIR at temp dirs per test."""
    monkeypatch.setenv("KLANGK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KLANGK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KLANGK_CUSTOMIZE_DIR", str(tmp_path / "customize"))
    monkeypatch.delenv("KLANGK_IMAGE_PULL_POLICY", raising=False)
    # Rebuild the DB instance from the new env so the engine cache and
    # db_path point at the per-test temp dir (#1452: no import-time globals
    # to rebind — construct a fresh DB from settings; #1520: bind it via the
    # ContextVar, not a module global).
    import klangk_backend.model as model
    from klangk_backend.model import db as db_mod

    _db_token = db_mod.set_current_db(db_mod.DB(KlangkSettings(os.environ)))
    # Clear agent caches so each test starts fresh.
    model.clear_agent_cache()
    yield tmp_path
    db_mod.reset_current_db(_db_token)


@pytest.fixture
async def db(temp_data_dir):
    """Initialize a fresh database."""
    import klangk_backend.model as model

    await model.init_db()
    return temp_data_dir


@pytest.fixture
async def agent_user(db):
    """Seed the chat agent user into the DB."""
    import klangk_backend.model as model

    async with model.transaction() as agent_db:
        await agent_db.execute(
            "INSERT OR REPLACE INTO users"
            " (id, email, password_hash, verified, provider, handle)"
            " VALUES (?, ?, NULL, 1, 'system', ?)",
            (model.AGENT_USER_ID, "clanker@example.com", "clanker"),
        )
    model.clear_agent_cache()


@pytest.fixture
async def user(db):
    """Create a test user and return it."""
    import klangk_backend.model as model

    user = await model.create_user(
        "testuser@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    return user


@pytest.fixture
async def admin_group(db):
    """Create the admin group and seed default ACLs."""
    import klangk_backend.model as model
    from klangk_backend.model import (
        ACTION_ALLOW,
        ACTION_DENY,
        PRINCIPAL_GROUP,
        PRINCIPAL_SYSTEM,
        SYSTEM_AUTHENTICATED,
        SYSTEM_EVERYONE,
    )

    group = await model.create_group("admin", description="Administrators")
    # Seed default ACLs
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
    await model.add_acl_entry(
        "/workspaces",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await model.add_acl_entry(
        "/admin",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_GROUP,
        group_id=group["id"],
    )
    await model.add_acl_entry(
        "/admin",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    return group


@pytest.fixture
async def admin_user(admin_group):
    """Create a test user in the admin group and return it."""
    import klangk_backend.model as model

    user = await model.create_user(
        "testadmin@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    await model.add_user_to_group(user["id"], admin_group["id"])
    return user


@pytest.fixture
def app_state(temp_data_dir):
    """Build a minimal app_state with owned instances wired (#1484).

    Shared across all test files — each test file that needs app_state
    requests it rather than defining its own. Built fresh per test from
    the temp_data_dir's settings so every owned instance points at the
    per-test tmp dir.
    """
    import types

    from klangk_backend.auth import Auth
    from klangk_backend.container import ContainerRegistry
    from klangk_backend.emailsvc import EmailService
    from klangk_backend.util import Util
    from klangk_backend.workspaces import Workspaces

    settings = make_settings(
        {
            "KLANGK_AUTH_MODES": "password",
            "KLANGK_DATA_DIR": str(temp_data_dir),
            "KLANGK_STATE_DIR": str(temp_data_dir / "state"),
            "KLANGK_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    state = types.SimpleNamespace(settings=settings)
    state.auth = Auth(state)
    registry = ContainerRegistry(state)
    state.container_registry = registry
    registry.app_state = state
    state.workspaces = Workspaces(state)
    state.email = EmailService(state)
    state.util = Util(state)
    # #1572: wire DB + Model(app_state) so converted domains (tokens,
    # login_attempts, invitations, ports) reached via app_state.model.*
    # resolve the per-test DB (the same one the ContextVar backstop binds
    # for the not-yet-converted domains).
    from _helpers import wire_db_and_model

    wire_db_and_model(state)
    # Resolve the instance ID into the per-test util (writes
    # <data_dir>/instance-id), so consumers of app_state.util.instance_id()
    # get a real value without each test calling resolve explicitly.
    state.util.resolve_instance_id()
    return state


@pytest.fixture
async def workspace(user):
    """Create a test workspace (without port allocation)."""
    import klangk_backend.model as model

    workspace = await model.create_workspace(user["id"], "test-workspace")
    return workspace
