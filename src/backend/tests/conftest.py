"""Shared fixtures for backend unit tests."""

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
    """Point BARK_DATA_DIR to a temp directory for each test."""
    monkeypatch.setenv("BARK_DATA_DIR", str(tmp_path))
    # Re-import to pick up the new env var
    import bark_backend.user_store as us
    import bark_backend.workspace_manager as wm

    us._data_dir = tmp_path
    us.DB_PATH = tmp_path / "bark.db"
    wm._data_dir = tmp_path
    wm.WORKSPACES_ROOT = tmp_path / "workspaces"
    return tmp_path


@pytest.fixture
async def db(temp_data_dir):
    """Initialize a fresh database."""
    import bark_backend.user_store as us

    await us.init_db()
    return temp_data_dir


@pytest.fixture
async def user(db):
    """Create a test user and return it."""
    import bark_backend.user_store as us

    user = await us.create_user(
        "testuser@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    return user


@pytest.fixture
async def admin_user(db):
    """Create a test user with admin role and return it."""
    import bark_backend.user_store as us

    user = await us.create_user(
        "testadmin@example.com", _TEST_PASSWORD_HASH, verified=True
    )
    await us.ensure_role("admin")
    await us.assign_role(user["id"], "admin")
    return user


@pytest.fixture
async def workspace(user):
    """Create a test workspace (without port allocation)."""
    import bark_backend.user_store as us

    workspace = await us.create_workspace(user["id"], "test-workspace")
    return workspace
