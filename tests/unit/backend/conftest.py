"""Shared fixtures for backend unit tests."""

import pytest


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path, monkeypatch):
    """Point BARK_DATA_DIR to a temp directory for each test."""
    monkeypatch.setenv("BARK_DATA_DIR", str(tmp_path))
    # Re-import to pick up the new env var
    import backend.user_store as us
    import backend.workspace_manager as wm

    us._data_dir = tmp_path
    us.DB_PATH = tmp_path / "bark.db"
    wm._data_dir = tmp_path
    wm.WORKSPACES_ROOT = tmp_path / "workspaces"
    return tmp_path


@pytest.fixture
async def db(temp_data_dir):
    """Initialize a fresh database."""
    import backend.user_store as us

    await us.init_db()
    return temp_data_dir


@pytest.fixture
async def user(db):
    """Create a test user and return it."""
    import backend.user_store as us
    import backend.auth as auth

    password_hash = auth._hash_password("testpass")
    user = await us.create_user("testuser", password_hash)
    return user


@pytest.fixture
async def workspace(user):
    """Create a test workspace (without port allocation)."""
    import backend.user_store as us

    workspace = await us.create_workspace(user["id"], "test-workspace")
    return workspace
