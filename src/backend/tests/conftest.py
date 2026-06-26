"""Shared fixtures for backend unit tests."""

import os

# Must be set before coverage.py initialises in each xdist worker so that
# code executed inside SQLAlchemy's greenlet context is tracked.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

import bcrypt

import pytest

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
def temp_data_dir(tmp_path, monkeypatch):
    """Point KLANGK_DATA_DIR to a temp directory for each test."""
    monkeypatch.setenv("KLANGK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KLANGK_IMAGE_PULL_POLICY", raising=False)
    # Re-import to pick up the new env var
    import klangk_backend.model as us
    import klangk_backend.workspaces as wm

    us._data_dir = tmp_path
    us.DB_PATH = tmp_path / "klangk.db"
    # Reset the SQLAlchemy engine so it reconnects to the new DB path.
    us._engine = None
    wm._data_dir = tmp_path
    wm.WORKSPACES_ROOT = tmp_path / "workspaces"
    # Clear agent caches so each test starts fresh.
    us.clear_agent_cache()
    return tmp_path


@pytest.fixture
async def db(temp_data_dir):
    """Initialize a fresh database."""
    import klangk_backend.model as us

    await us.init_db()
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
            (us.AGENT_USER_ID, "MrBoops@example.com", "MrBoops"),
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
async def workspace(user):
    """Create a test workspace (without port allocation)."""
    import klangk_backend.model as us

    workspace = await us.create_workspace(user["id"], "test-workspace")
    return workspace
