"""Shared fixtures for backend unit tests."""

import os
import tempfile

# Must be set before coverage.py initialises in each xdist worker so that
# code executed inside SQLAlchemy's greenlet context is tracked.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

# state_dir / data_dir are required (no defaults, #1461). auth.py still reads
# config at import time (#1501 — Auth promotion not yet done); set temp dirs
# in the env before any such import runs during collection. The autouse
# `temp_data_dir` fixture overrides these with per-test tmp dirs once tests
# start. util.py no longer triggers this (#1503), but auth.py does.
os.environ.setdefault(
    "KLANGK_STATE_DIR", tempfile.mkdtemp(prefix="klangk-collect-state-")
)
os.environ.setdefault(
    "KLANGK_DATA_DIR", tempfile.mkdtemp(prefix="klangk-collect-data-")
)

import bcrypt

import pytest

from klangk_backend.settings import KlangkSettings

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
def _default_auth_mode(monkeypatch):
    """Pin the test suite to ``password`` mode.

    The production default for ``KLANGK_AUTH_MODES`` (when unset and no OIDC
    is configured) is ``none`` — see ``oidc.auth_modes``. Most backend tests
    exercise the authenticated API via ``_auth_headers`` (HTTP login), which
    the ``password`` gate admits; pinning the suite here keeps that working
    regardless of how the production default evolves, so a default change
    doesn't silently flip ~190 login-flow tests.

    Tests that care about a *specific* mode set it themselves (their
    ``setenv``/``delenv`` overrides this). ``delenv`` is the way to opt into
    the real production default within a test.
    """
    monkeypatch.setenv("KLANGK_AUTH_MODES", "password")


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point KLANGK_DATA_DIR / KLANGK_STATE_DIR / KLANGK_CUSTOMIZE_DIR at temp dirs per test."""
    monkeypatch.setenv("KLANGK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KLANGK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KLANGK_CUSTOMIZE_DIR", str(tmp_path / "customize"))
    monkeypatch.delenv("KLANGK_IMAGE_PULL_POLICY", raising=False)
    # Rebuild the DB instance from the new env so the engine cache and
    # db_path point at the per-test temp dir (#1452: no import-time globals
    # to rebind — construct a fresh DB from settings).
    import klangk_backend.model as us
    import klangk_backend.model.db as us_core

    us_core.set_db(us_core.DB(KlangkSettings(os.environ)))
    # Clear agent caches so each test starts fresh.
    us.clear_agent_cache()
    # Set a deterministic instance ID for tests that don't use the db
    # fixture.  The db fixture overwrites this with a DB-resolved value.
    import klangk_backend.model.instance as inst

    inst._cache = "test"
    return tmp_path


@pytest.fixture
async def db(temp_data_dir):
    """Initialize a fresh database and resolve instance ID."""
    import klangk_backend.model as us

    await us.init_db()
    await us.resolve_instance_id()
    return temp_data_dir


@pytest.fixture
async def agent_user(db):
    """Seed the chat agent user into the DB."""
    import klangk_backend.model as us

    async with us.transaction() as agent_db:
        await agent_db.execute(
            "INSERT OR REPLACE INTO users"
            " (id, email, password_hash, verified, provider, handle)"
            " VALUES (?, ?, NULL, 1, 'system', ?)",
            (us.AGENT_USER_ID, "clanker@example.com", "clanker"),
        )
    us.clear_agent_cache()


@pytest.fixture
async def user(db):
    """Create a test user and return it."""
    import klangk_backend.model as us

    user = await us.create_user(
        "testuser@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    return user


@pytest.fixture
async def admin_group(db):
    """Create the admin group and seed default ACLs."""
    import klangk_backend.model as us
    from klangk_backend.model import (
        ACTION_ALLOW,
        ACTION_DENY,
        PRINCIPAL_GROUP,
        PRINCIPAL_SYSTEM,
        SYSTEM_AUTHENTICATED,
        SYSTEM_EVERYONE,
    )

    group = await us.create_group("admin", description="Administrators")
    # Seed default ACLs
    await us.add_acl_entry(
        "/",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await us.add_acl_entry(
        "/",
        1,
        ACTION_DENY,
        "*",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_EVERYONE,
    )
    await us.add_acl_entry(
        "/workspaces",
        0,
        ACTION_ALLOW,
        "create",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    await us.add_acl_entry(
        "/admin",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_GROUP,
        group_id=group["id"],
    )
    await us.add_acl_entry(
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
    import klangk_backend.model as us

    user = await us.create_user(
        "testadmin@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    await us.add_user_to_group(user["id"], admin_group["id"])
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

    from klangk_backend.settings import KlangkSettings
    from klangk_backend.container import ContainerRegistry
    from klangk_backend.emailsvc import EmailService
    from klangk_backend.util import Util
    from klangk_backend.workspaces import Workspaces

    settings = KlangkSettings(os.environ)
    state = types.SimpleNamespace(settings=settings)
    registry = ContainerRegistry(state)
    state.container_registry = registry
    registry.app_state = state
    state.workspaces = Workspaces(state)
    state.email = EmailService(state)
    state.util = Util(state)
    return state


@pytest.fixture
async def workspace(user):
    """Create a test workspace (without port allocation)."""
    import klangk_backend.model as us

    workspace = await us.create_workspace(user["id"], "test-workspace")
    return workspace
