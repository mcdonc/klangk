"""Direct coverage for ``WorkspacesModel(app_state)`` (#1575).

Exercises every method on ``app_state.state.model.workspaces`` — the
app_state-owned form app code migrated to — including the cross-domain
shared-workspace listing (which reaches ``app_state.state.model.users``) and
the agent-principal / setup-state guards. Mirrors the #1573
``test_model_users.py`` pattern: ``app_state`` (db + model wired via the
ContextVar DB) with the schema initialized.
"""

import asyncio

import pytest

from klangk.model.acl import ACTION_ALLOW, PRINCIPAL_USER
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


async def test_allowed_domains_roundtrip(ws, user):
    # Create persists the list; get/get_by_id/list all surface it.
    ws_row = await ws.create_workspace_with_acl(
        user["id"],
        "filtered",
        allowed_domains=["github.com:443", "pypi.org"],
    )
    assert ws_row["allowed_domains"] == ["github.com:443", "pypi.org"]
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["allowed_domains"] == ["github.com:443", "pypi.org"]
    by_id = await ws.get_workspace_by_id(ws_row["id"])
    assert by_id["allowed_domains"] == ["github.com:443", "pypi.org"]
    listing = await ws.list_workspaces(user["id"])
    assert listing["items"][0]["allowed_domains"] == [
        "github.com:443",
        "pypi.org",
    ]


async def test_allowed_domains_default_none(ws, user):
    # No allowed_domains -> NULL -> unrestricted (surfaces as None).
    ws_row = await ws.create_workspace(user["id"], "open")
    assert ws_row["allowed_domains"] is None
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["allowed_domains"] is None


async def test_allowed_domains_updateable(ws, user):
    ws_row = await ws.create_workspace(user["id"], "editable")
    assert (
        await ws.update_workspace(
            ws_row["id"],
            user["id"],
            allowed_domains=["github.com"],
        )
        is True
    )
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["allowed_domains"] == ["github.com"]
    # Clearing by passing None restores unrestricted.
    assert (
        await ws.update_workspace(
            ws_row["id"], user["id"], allowed_domains=None
        )
        is True
    )
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["allowed_domains"] is None


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


# --- per-workspace settings bag (#864) ---


SETTINGS_BAG = {
    "idle_timeout": 300,
    "bridge_timeout": 60,
    "cpu_limit": 1.5,
    "memory_limit": "2g",
    "pids_limit": 512,
}


async def test_settings_roundtrip(ws, user):
    # Create persists the bag; get/get_by_id/list all surface it.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "tuned", settings=SETTINGS_BAG
    )
    assert ws_row["settings"] == SETTINGS_BAG
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] == SETTINGS_BAG
    by_id = await ws.get_workspace_by_id(ws_row["id"])
    assert by_id["settings"] == SETTINGS_BAG
    listing = await ws.list_workspaces(user["id"])
    assert listing["items"][0]["settings"] == SETTINGS_BAG


async def test_settings_default_none(ws, user):
    # No settings -> NULL -> surfaces as None (no overrides).
    ws_row = await ws.create_workspace(user["id"], "plain")
    assert ws_row["settings"] is None
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] is None
    by_id = await ws.get_workspace_by_id(ws_row["id"])
    assert by_id["settings"] is None
    listing = await ws.list_workspaces(user["id"])
    assert listing["items"][0]["settings"] is None


async def test_settings_in_shared_listing(ws, app_state, user):
    # list_shared_workspaces surfaces the bag too.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "shared-tuned", settings={"idle_timeout": 120}
    )
    other = await app_state.state.model.users.create_user("other@x.com", "pw")
    resource = f"/workspaces/{ws_row['id']}"
    await app_state.state.model.acl.add_acl_entry(
        resource,
        100,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_USER,
        user_id=other["id"],
    )
    shared = await ws.list_shared_workspaces(other["id"])
    assert shared["items"][0]["settings"] == {"idle_timeout": 120}


async def test_settings_updateable_via_full_replace(ws, user):
    ws_row = await ws.create_workspace(user["id"], "editable")
    assert (
        await ws.update_workspace(
            ws_row["id"],
            user["id"],
            settings={"idle_timeout": 300, "cpu_limit": 2},
        )
        is True
    )
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] == {"idle_timeout": 300, "cpu_limit": 2}
    # Full-replace: setting settings=None clears the whole bag.
    assert (
        await ws.update_workspace(ws_row["id"], user["id"], settings=None)
        is True
    )
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] is None


async def test_update_workspace_settings_merge(ws, user):
    # PATCH-style read-modify-write merge.
    ws_row = await ws.create_workspace(
        user["id"], "mergeable", settings={"idle_timeout": 300}
    )
    # Add a key + replace an existing one.
    merged = await ws.update_workspace_settings(
        ws_row["id"],
        user["id"],
        {"idle_timeout": 600, "cpu_limit": 1.5},
    )
    assert merged == {"idle_timeout": 600, "cpu_limit": 1.5}
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] == {"idle_timeout": 600, "cpu_limit": 1.5}


async def test_update_workspace_settings_delete_key(ws, user):
    ws_row = await ws.create_workspace(
        user["id"],
        "deletable",
        settings={"idle_timeout": 300, "cpu_limit": 1.5},
    )
    # None value deletes just that key (reverts to deploy default).
    merged = await ws.update_workspace_settings(
        ws_row["id"],
        user["id"],
        {"idle_timeout": None},
    )
    assert merged == {"cpu_limit": 1.5}


async def test_update_workspace_settings_delete_last_key_nulls_column(
    ws, user
):
    ws_row = await ws.create_workspace(
        user["id"], "last-key", settings={"idle_timeout": 300}
    )
    merged = await ws.update_workspace_settings(
        ws_row["id"],
        user["id"],
        {"idle_timeout": None},
    )
    # Bag is now empty -> column NULL -> None.
    assert merged is None
    got = await ws.get_workspace(ws_row["id"], user["id"])
    assert got["settings"] is None


async def test_update_workspace_settings_missing_workspace(ws, user):
    # Nonexistent workspace / wrong owner -> None.
    assert (
        await ws.update_workspace_settings(
            "missing", user["id"], {"idle_timeout": 300}
        )
        is None
    )


async def test_update_workspace_settings_concurrent_no_lost_update(ws, user):
    # Regression for the read-modify-write lost-update on the settings bag
    # (#1951 review, I1): concurrent PATCHes to *different* keys must all
    # land, not last-writer-wins on the whole blob. The merge uses
    # compare-and-swap (UPDATE ... WHERE settings IS <old_blob>), so a
    # concurrent writer that changed the blob between our SELECT and UPDATE
    # is detected via rowcount=0 and the patch is re-read + re-merged on the
    # latest base. NullPool gives each transaction a fresh connection to the
    # same on-disk SQLite file, so the SELECT/UPDATE interleaving is real.
    ws_row = await ws.create_workspace(
        user["id"], "concurrent", settings={"idle_timeout": 300}
    )
    ws_id = ws_row["id"]
    uid = user["id"]
    # Five patches to five distinct keys, fired concurrently.
    await asyncio.gather(
        ws.update_workspace_settings(ws_id, uid, {"cpu_limit": 1.5}),
        ws.update_workspace_settings(ws_id, uid, {"pids_limit": 256}),
        ws.update_workspace_settings(ws_id, uid, {"memory_limit": "512m"}),
        ws.update_workspace_settings(ws_id, uid, {"bridge_timeout": 60}),
        ws.update_workspace_settings(ws_id, uid, {"idle_timeout": 0}),
    )
    got = await ws.get_workspace(ws_id, uid)
    # Every override survived — no key was dropped by a concurrent writer.
    # (Without CAS, the last writer's stale-base blob would clobber the
    # others and only its own key + the original idle_timeout would remain.)
    assert got["settings"] == {
        "idle_timeout": 0,
        "cpu_limit": 1.5,
        "pids_limit": 256,
        "memory_limit": "512m",
        "bridge_timeout": 60,
    }


async def test_settings_in_auto_start_listing(ws, user):
    await ws.create_workspace(
        user["id"],
        "auto-tuned",
        auto_start=True,
        settings={"idle_timeout": 120},
    )
    started = await ws.list_auto_start_workspaces()
    assert started[0]["settings"] == {"idle_timeout": 120}
