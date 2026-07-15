"""Direct coverage for ``UsersModel(app_state)`` (#1573).

Exercises every method on ``app_state.model.users`` — the app_state-owned
form app code is migrating to — plus the db-param helpers and the
agent-user cache. Mirrors the #1572 ``test_model_app_state.py`` pattern:
``app_state`` (db + model wired via the ContextVar DB) with the schema
initialized.
"""

import pytest

from klangk_backend.model.users import (
    AGENT_USER_ID,
    AgentPrincipalError,
)


@pytest.fixture
async def users(app_state, db):
    """``app_state.model.users`` with the schema initialized."""
    return app_state.model.users


async def test_create_and_get_user(users):
    u = await users.create_user("a@x.com", "hash", verified=True)
    assert u["email"] == "a@x.com"
    assert u["verified"] is True
    by_email = await users.get_user_by_email("a@x.com")
    assert by_email["id"] == u["id"]
    by_id = await users.get_user_by_id(u["id"])
    assert by_id["handle"] == u["handle"]
    assert await users.get_user_by_id("nope") is None
    assert await users.get_user_by_email("missing@x.com") is None


async def test_get_user_by_handle_and_handle(users):
    u = await users.create_user("b@x.com", "hash")
    assert await users.get_user_handle(u["id"]) == u["handle"]
    by_handle = await users.get_user_by_handle(u["handle"])
    assert by_handle["id"] == u["id"]
    assert await users.get_user_by_handle("nope") is None
    assert await users.get_user_handle("nope") is None


async def test_set_user_handle(users):
    u = await users.create_user("c@x.com", "hash")
    await users.set_user_handle(u["id"], "newhandle")
    assert await users.get_user_handle(u["id"]) == "newhandle"
    with pytest.raises(ValueError):
        await users.set_user_handle(u["id"], "UPPER")


async def test_link_oidc_and_external_id_and_verify(users):
    u = await users.create_user("d@x.com", "hash")
    await users.link_oidc_identity(u["id"], "google", "ext-1")
    ext = await users.get_user_by_external_id("google", "ext-1")
    assert ext["id"] == u["id"]
    assert ext["verified"] is False
    assert await users.get_user_by_external_id("google", "missing") is None
    assert await users.verify_user(u["id"]) is True
    assert await users.verify_user("missing") is False


async def test_insert_unverified_user(users):
    async with users.app_state.db.transaction() as db:
        handle = await users.insert_unverified_user(
            db, "uid-uv", "uv@x.com", "hash"
        )
    assert handle
    fetched = await users.get_user_by_email("uv@x.com")
    assert fetched["id"] == "uid-uv"
    assert fetched["verified"] is False


async def test_create_group_and_lookup(users):
    g = await users.create_group("g1", description="d")
    by_name = await users.get_group_by_name("g1")
    assert by_name["id"] == g["id"]
    by_id = await users.get_group_by_id(g["id"])
    assert by_id["name"] == "g1"
    assert await users.get_group_by_name("missing") is None
    assert await users.get_group_by_id("missing") is None


async def test_list_update_delete_group(users):
    g = await users.create_group("g2")
    listed = await users.list_groups()
    assert any(gr["id"] == g["id"] for gr in listed["groups"])
    listed_q = await users.list_groups(q="g2")
    assert listed_q["total"] >= 1
    assert await users.update_group(g["id"], name="g2b") is True
    updated = await users.get_group_by_id(g["id"])
    assert updated["name"] == "g2b"
    assert await users.update_group(g["id"]) is False  # no fields
    assert await users.delete_group(g["id"]) is True
    assert await users.delete_group(g["id"]) is False


async def test_group_membership(users):
    u = await users.create_user("m@x.com", "hash")
    g = await users.create_group("mg")
    await users.add_user_to_group(u["id"], g["id"])
    # idempotent
    await users.add_user_to_group(u["id"], g["id"])
    assert g["id"] in await users.get_user_group_ids(u["id"])
    groups = await users.get_user_groups(u["id"])
    assert any(gr["id"] == g["id"] for gr in groups)
    members = await users.get_group_members(g["id"])
    assert any(mm["id"] == u["id"] for mm in members)
    assert await users.remove_user_from_group(u["id"], g["id"]) is True
    assert await users.remove_user_from_group(u["id"], g["id"]) is False


async def test_oidc_sync_group_ids(users):
    u = await users.create_user("o@x.com", "hash")
    g = await users.create_group("og")
    await users.add_user_to_group(u["id"], g["id"], source="oidc_sync")
    other = await users.create_group("m")
    await users.add_user_to_group(u["id"], other["id"])
    assert g["id"] in await users.get_user_oidc_sync_group_ids(u["id"])


async def test_list_users_search_delete(users):
    u = await users.create_user("l1@x.com", "hash")
    await users.create_user("l2@x.com", "hash")
    listed = await users.list_users(q="l1")
    assert listed["total"] >= 1
    all_listed = await users.list_users()
    assert all_listed["total"] >= 2
    found = await users.search_users("l1")
    assert any(f["id"] == u["id"] for f in found)
    assert await users.delete_user(u["id"]) is True
    assert await users.delete_user(u["id"]) is False


async def test_update_email_and_password(users):
    u = await users.create_user("u@x.com", "hash")
    await users.update_email(u["id"], "u2@x.com")
    assert (await users.get_user_by_id(u["id"]))["email"] == "u2@x.com"
    await users.update_password(u["id"], "newhash")
    fetched = await users.get_user_by_email("u2@x.com")
    assert fetched["password_hash"] == "newhash"


async def test_agent_principal_guards(users):
    with pytest.raises(AgentPrincipalError):
        await users.add_user_to_group(AGENT_USER_ID, "gid")
    with pytest.raises(AgentPrincipalError):
        await users.delete_user(AGENT_USER_ID)
    with pytest.raises(AgentPrincipalError):
        await users.update_email(AGENT_USER_ID, "x@x.com")
    with pytest.raises(AgentPrincipalError):
        await users.update_password(AGENT_USER_ID, "h")


async def test_agent_user_cache(users, agent_user):
    users.clear_agent_cache()
    au = await users.get_agent_user()
    assert au["id"] == AGENT_USER_ID
    # cached on second call
    assert await users.get_agent_user() == au
    assert await users.agent_handle() == au["handle"]
    assert await users.agent_email() == au["email"]
    users.clear_agent_cache()


async def test_agent_user_unseeded_fallback(users):
    users.clear_agent_cache()
    # No agent row: get_user_by_id returns None -> fallback defaults.
    au = await users.get_agent_user()
    assert au["id"] == AGENT_USER_ID
    assert au["handle"]  # fallback handle
    users.clear_agent_cache()


async def test_assert_handle_not_agent(users, agent_user):
    users.clear_agent_cache()
    handle = await users.agent_handle()
    u = await users.create_user("ag@x.com", "hash")
    with pytest.raises(ValueError):
        await users.set_user_handle(u["id"], handle)
    users.clear_agent_cache()


async def test_db_param_handle_helpers(users):
    u = await users.create_user("h@x.com", "hash")
    base = (await users.get_user_handle(u["id"])) or "handle"
    async with users.app_state.db.transaction() as db:
        uniq = await users.unique_handle(db, base)
        gen = await users.generate_handle(db, "new@email.com")
    assert uniq  # base taken by the user -> suffixed or hashed
    assert gen


async def test_backfill_handles_method(users):
    async with users.app_state.db.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified, handle)"
            " VALUES (?, ?, ?, 0, NULL)",
            ("bf-id", "bf@x.com", "h"),
        )
        await users.backfill_handles(db)
    fetched = await users.get_user_by_id("bf-id")
    assert fetched["handle"] is not None
