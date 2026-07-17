"""Direct coverage for ``WorkspacesModel(app_state)`` (#1575).

Exercises every method on ``app_state.state.model.workspaces`` — the
app_state-owned form app code migrated to — including the cross-domain
shared-workspace listing (which reaches ``app_state.state.model.users``) and
the agent-principal / setup-state guards. Mirrors the #1573
``test_model_users.py`` pattern: ``app_state`` (db + model wired via the
ContextVar DB) with the schema initialized.
"""

import pytest

from klangk.model.workspaces import (
    SETUP_STATE_COMPLETE,
    SETUP_STATE_PENDING,
)
from klangk.model.users import AGENT_USER_ID, AgentPrincipalError


@pytest.fixture
async def ws(app_state, db):
    """``app_state.state.model.workspaces`` with the schema initialized."""
    return app_state.state.model.workspaces


async def test_create_workspace_with_acl_and_get(ws, user):
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "owned", setup_state=SETUP_STATE_COMPLETE
    )
    assert ws_row["user_id"] == user["id"]
    assert ws_row["num_ports"] is not None
    # Seeded owner ACE + role groups are visible via the members/acl path.
    by_id = await ws.get_workspace_by_id(ws_row["id"])
    assert by_id["name"] == "owned"
    assert await ws.get_workspace_by_id("missing") is None


async def test_create_workspace_row_only(ws, user):
    ws_row = await ws.create_workspace(user["id"], "row-only")
    assert ws_row["name"] == "row-only"
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["id"] == ws_row["id"]


async def test_create_workspace_with_acl_rejects_agent(ws):
    with pytest.raises(AgentPrincipalError):
        await ws.create_workspace_with_acl(AGENT_USER_ID, "agent-owned")


async def test_create_invalid_setup_state(ws, user):
    with pytest.raises(ValueError):
        await ws.create_workspace_with_acl(
            user["id"], "bad", setup_state="bogus"
        )
    with pytest.raises(ValueError):
        await ws.create_workspace(user["id"], "bad", setup_state="bogus")


async def test_list_workspaces_with_query(ws, user):
    await ws.create_workspace(user["id"], "alpha")
    await ws.create_workspace(user["id"], "beta")
    filtered = await ws.list_workspaces(user["id"], q="alp")
    assert [w["name"] for w in filtered["items"]] == ["alpha"]
    all_items = await ws.list_workspaces(user["id"])
    assert {w["name"] for w in all_items["items"]} == {"alpha", "beta"}


async def test_list_shared_workspaces(ws, app_state, user):
    other = await app_state.state.model.users.create_user("other@x.com", "h")
    ws_row = await ws.create_workspace_with_acl(other["id"], "shared-ws")
    # Grant ``user`` a direct user-level Allow ACE on the workspace.
    from klangk.model import ACTION_ALLOW, PRINCIPAL_USER

    await app_state.state.model.acl.add_acl_entry(
        f"/workspaces/{ws_row['id']}",
        100,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    shared = await ws.list_shared_workspaces(user["id"])
    assert any(w["id"] == ws_row["id"] for w in shared["items"])
    assert shared["items"][0]["owner_email"] == "other@x.com"
    # q-filter narrows by name.
    filtered = await ws.list_shared_workspaces(user["id"], q="shared")
    assert [w["name"] for w in filtered["items"]] == ["shared-ws"]
    # Nothing shared with ``other``.
    assert (await ws.list_shared_workspaces(other["id"]))["items"] == []


async def test_get_workspace_access_control(ws, user):
    ws_row = await ws.create_workspace(user["id"], "mine")
    assert await ws.get_workspace(ws_row["id"], user["id"]) is not None
    # Wrong user -> not found.
    assert await ws.get_workspace(ws_row["id"], "someone-else") is None
    assert await ws.get_workspace("missing", user["id"]) is None
    # No user_id -> no access check.
    assert (await ws.get_workspace(ws_row["id"]))["id"] == ws_row["id"]


async def test_get_workspace_members(ws, app_state, user):
    other = await app_state.state.model.users.create_user("member@x.com", "h")
    ws_row = await ws.create_workspace_with_acl(user["id"], "members-ws")
    from klangk.model import ACTION_ALLOW, PRINCIPAL_USER

    await app_state.state.model.acl.add_acl_entry(
        f"/workspaces/{ws_row['id']}",
        100,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_USER,
        user_id=other["id"],
    )
    members = await ws.get_workspace_members(ws_row["id"])
    assert [m["id"] for m in members] == [other["id"]]
    # Owner is excluded from the member list.
    assert all(m["id"] != user["id"] for m in members)


async def test_delete_workspace_with_role_groups(ws, user):
    ws_row = await ws.create_workspace_with_acl(user["id"], "to-delete")
    # Seeded role groups exist; delete must tear them down.
    assert await ws.delete_workspace(ws_row["id"], user["id"]) is True
    assert await ws.get_workspace_by_id(ws_row["id"]) is None
    # Second delete (gone) -> False.
    assert await ws.delete_workspace(ws_row["id"], user["id"]) is False
    # Wrong owner -> False.
    ws2 = await ws.create_workspace(user["id"], "another")
    assert await ws.delete_workspace(ws2["id"], "wrong-owner") is False


async def test_update_workspace_container(ws, user):
    ws_row = await ws.create_workspace(user["id"], "container-ws")
    await ws.update_workspace_container(ws_row["id"], "cid-1")
    assert (await ws.get_workspace_by_id(ws_row["id"]))[
        "container_id"
    ] == "cid-1"
    await ws.update_workspace_container(ws_row["id"], None)
    assert (await ws.get_workspace_by_id(ws_row["id"]))["container_id"] is None


async def test_update_workspace_fields(ws, user):
    ws_row = await ws.create_workspace(user["id"], "updatable")
    assert (
        await ws.update_workspace(
            ws_row["id"],
            user["id"],
            name="renamed",
            setup_state=SETUP_STATE_PENDING,
            auto_start=True,
            mounts=["/m"],
            env={"K": "v"},
        )
        is True
    )
    got = await ws.get_workspace(ws_row["id"])
    assert got["name"] == "renamed"
    assert got["setup_state"] == SETUP_STATE_PENDING
    assert got["auto_start"] is True
    assert got["mounts"] == ["/m"]
    assert got["env"] == {"K": "v"}
    # Unknown fields are ignored; no-op update returns False.
    assert (
        await ws.update_workspace(ws_row["id"], user["id"], bogus="x") is False
    )
    # Invalid setup_state raises.
    with pytest.raises(ValueError):
        await ws.update_workspace(
            ws_row["id"], user["id"], setup_state="bogus"
        )
    # Wrong owner -> False.
    assert (
        await ws.update_workspace(ws_row["id"], "wrong", name="nope") is False
    )


async def test_transfer_workspace(ws, app_state, user):
    new_owner = await app_state.state.model.users.create_user("new@x.com", "h")
    ws_row = await ws.create_workspace_with_acl(user["id"], "transfer-me")
    transferred = await ws.transfer_workspace(ws_row["id"], new_owner["id"])
    assert transferred["user_id"] == new_owner["id"]
    # Owner ACE + owners-group membership moved to the new owner.
    entries = await app_state.state.model.acl.get_acl_entries(
        f"/workspaces/{ws_row['id']}"
    )
    owner_ace = next(
        e for e in entries if e["position"] == 0 and e["permission"] == "*"
    )
    assert owner_ace["user_id"] == new_owner["id"]


async def test_transfer_workspace_guards(ws, app_state, user):
    new_owner = await app_state.state.model.users.create_user(
        "new2@x.com", "h"
    )
    ws_row = await ws.create_workspace_with_acl(user["id"], "guard-me")
    # Agent principal cannot receive a workspace.
    with pytest.raises(AgentPrincipalError):
        await ws.transfer_workspace(ws_row["id"], AGENT_USER_ID)
    # Already the owner.
    with pytest.raises(ValueError, match="already the owner"):
        await ws.transfer_workspace(ws_row["id"], user["id"])
    # Duplicate name in target owner's set.
    await ws.create_workspace_with_acl(new_owner["id"], "guard-me")
    with pytest.raises(ValueError, match="already owns"):
        await ws.transfer_workspace(ws_row["id"], new_owner["id"])
    # Nonexistent workspace -> None.
    assert await ws.transfer_workspace("missing", new_owner["id"]) is None


async def test_get_user_workspaces_with_containers(ws, user):
    assert await ws.get_user_workspaces_with_containers(user["id"]) == []
    ws_row = await ws.create_workspace(user["id"], "with-container")
    await ws.update_workspace_container(ws_row["id"], "cid-x")
    result = await ws.get_user_workspaces_with_containers(user["id"])
    assert [w["container_id"] for w in result] == ["cid-x"]


async def test_list_auto_start_workspaces(ws, user):
    await ws.create_workspace(user["id"], "manual")
    # auto_start lives on the container/image config; set it via update.
    auto = await ws.create_workspace(user["id"], "auto")
    await ws.update_workspace(auto["id"], user["id"], auto_start=True)
    started = await ws.list_auto_start_workspaces()
    assert [w["name"] for w in started] == ["auto"]
    assert started[0]["auto_start"] is True
