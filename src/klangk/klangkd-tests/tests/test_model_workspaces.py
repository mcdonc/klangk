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


async def test_existing_workspace_ids(ws, user):
    # Basis for the orphan sidecar-token sweep: the set reflects the live
    # workspaces table, and a delete drops the id (#2309).
    a = await ws.create_workspace(user["id"], "alpha")
    b = await ws.create_workspace(user["id"], "beta")
    ids = await ws.existing_workspace_ids()
    assert {a["id"], b["id"]} <= ids
    await ws.delete_workspace(a["id"], user["id"])
    ids_after = await ws.existing_workspace_ids()
    assert a["id"] not in ids_after
    assert b["id"] in ids_after


# -- consent pause (#2332) --


async def test_get_consent_pause_default_none(ws, user):
    row = await ws.create_workspace(user["id"], "pause-default")
    assert await ws.get_consent_pause(row["id"]) is None


async def test_set_and_get_consent_pause(ws, user):
    row = await ws.create_workspace(user["id"], "pause-set")
    assert await ws.set_consent_pause(row["id"], 1234.5) is True
    assert await ws.get_consent_pause(row["id"]) == 1234.5


async def test_set_consent_pause_clear_with_none(ws, user):
    row = await ws.create_workspace(user["id"], "pause-clear")
    await ws.set_consent_pause(row["id"], 9999.0)
    assert await ws.get_consent_pause(row["id"]) == 9999.0
    assert await ws.set_consent_pause(row["id"], None) is True
    assert await ws.get_consent_pause(row["id"]) is None


async def test_set_consent_pause_missing_workspace_false(ws):
    assert await ws.set_consent_pause("no-such-ws", 1234.0) is False


async def test_get_consent_pause_missing_workspace_none(ws):
    assert await ws.get_consent_pause("no-such-ws") is None


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


async def test_per_handle_home_roundtrip(ws, user):
    # #2719: default is per-handle (today's layout); an explicit False
    # persists and round-trips through every full-row read.
    default = await ws.create_workspace_with_acl(user["id"], "default-home")
    assert default["per_handle_home"] is True
    shared = await ws.create_workspace(
        user["id"], "shared-home", per_handle_home=False
    )
    assert shared["per_handle_home"] is False
    got = await ws.get_workspace_by_id(shared["id"])
    assert got["per_handle_home"] is False
    listed = await ws.list_workspaces(user["id"], q="shared-home")
    assert listed["items"][0]["per_handle_home"] is False


async def test_create_rejects_non_bool_per_handle_home(ws, user):
    with pytest.raises(ValueError):
        await ws.create_workspace_with_acl(
            user["id"], "bad", per_handle_home=1
        )
    with pytest.raises(ValueError):
        await ws.create_workspace(user["id"], "bad", per_handle_home=0)


async def test_update_workspace_flips_per_handle_home(ws, user):
    # Editable after create (#2719): a PUT of only this field updates
    # the row and the new value round-trips.
    created = await ws.create_workspace(
        user["id"], "flippable", per_handle_home=False
    )
    updated = await ws.update_workspace(
        created["id"], user["id"], per_handle_home=True
    )
    assert updated is True
    got = await ws.get_workspace_by_id(created["id"])
    assert got["per_handle_home"] is True
    # And back.
    await ws.update_workspace(created["id"], user["id"], per_handle_home=False)
    got = await ws.get_workspace_by_id(created["id"])
    assert got["per_handle_home"] is False


# -- classification marking (#2768) --


async def test_classification_banner_roundtrip(ws, user):
    created = await ws.create_workspace_with_acl(
        user["id"], "marked", classification_banner="  SECRET  "
    )
    # Whitespace is stripped at create; the row and every read path
    # (get / list / shared list) carry the normalized value.
    assert created["classification_banner"] == "SECRET"
    got = await ws.get_workspace_by_id(created["id"])
    assert got["classification_banner"] == "SECRET"
    listed = await ws.list_workspaces(user["id"])
    assert listed["items"][0]["classification_banner"] == "SECRET"
    # Default is NULL (inherit the deploy default at display time).
    bare = await ws.create_workspace(user["id"], "unmarked")
    assert bare["classification_banner"] is None


async def test_classification_banner_empty_normalizes_to_inherit(ws, user):
    created = await ws.create_workspace(user["id"], "inherit", "")
    assert created["classification_banner"] is None
    got = await ws.get_workspace_by_id(created["id"])
    assert got["classification_banner"] is None


async def test_update_workspace_sets_and_clears_classification_banner(
    ws, user
):
    created = await ws.create_workspace(user["id"], "mark-edit")
    assert await ws.update_workspace(
        created["id"], user["id"], classification_banner="CUI"
    )
    got = await ws.get_workspace_by_id(created["id"])
    assert got["classification_banner"] == "CUI"
    # An emptied value clears the override (back to inherit).
    assert await ws.update_workspace(
        created["id"], user["id"], classification_banner="  "
    )
    got = await ws.get_workspace_by_id(created["id"])
    assert got["classification_banner"] is None


async def test_create_rejects_bad_classification_banner(ws, user):
    with pytest.raises(ValueError, match="at most"):
        await ws.create_workspace(
            user["id"], "too-long", classification_banner="X" * 121
        )
    with pytest.raises(ValueError, match="single line"):
        await ws.create_workspace(
            user["id"], "newline", classification_banner="TOP\nSECRET"
        )
    with pytest.raises(ValueError, match="must be a string"):
        await ws.create_workspace(user["id"], "int", classification_banner=5)


async def test_update_rejects_bad_classification_banner(ws, user):
    created = await ws.create_workspace(user["id"], "mark-bad")
    with pytest.raises(ValueError, match="single line"):
        await ws.update_workspace(
            created["id"], user["id"], classification_banner="A\x00B"
        )


def test_normalize_classification_banner_unit():
    from klangk.model.workspaces import normalize_classification_banner

    assert normalize_classification_banner(None) is None
    assert normalize_classification_banner("") is None
    assert normalize_classification_banner("   ") is None
    assert normalize_classification_banner(" CUI ") == "CUI"
    # 120 is the ceiling; 121 rejects.
    assert len(normalize_classification_banner("X" * 120)) == 120
    with pytest.raises(ValueError):
        normalize_classification_banner("X" * 121)


def test_normalize_rejects_invisible_format_characters():
    """#2768 review: a marking is a security label — bidi overrides and
    zero-width characters could make the banner *display* as a different
    marking than the DB records, and NEL/Zl/Zp break the one-line layout.
    All must reject with the format-character message."""
    from klangk.model.workspaces import normalize_classification_banner

    for bad in (
        "TOP\u202eSECRET",  # RTL override — renders reversed
        "CUI\u200b",  # zero-width space
        "A\u0085B",  # NEL (Unicode line break, category Cc)
        "A\u2028B",  # line separator (Zl)
        "A\u2029B",  # paragraph separator (Zp)
        "A\u00adB",  # soft hyphen (Cf)
        "A\ufeffB",  # BOM (Cf)
        "A\u2066B",  # left-to-right isolate (Cf)
    ):
        with pytest.raises(ValueError, match="invisible format"):
            normalize_classification_banner(bad)
    # Printable non-ASCII stays allowed (accented/site-specific labels).
    assert normalize_classification_banner("CUI//FOUO Ünïcode") == (
        "CUI//FOUO Ünïcode"
    )


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


async def test_add_allowed_domain_appends_lowercased_deduped(ws, user):
    # A forever consent allow persists by mutating allowed_domains (#2368).
    # New entries are lowercased + de-duplicated; an existing entry (even with
    # different case) is a no-op (idempotent).
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "forever", allowed_domains=["PyPI.org:443"]
    )
    assert await ws.add_allowed_domain(ws_row["id"], "github.com:443") is True
    assert (
        await ws.add_allowed_domain(ws_row["id"], "GITHUB.COM:443") is True
    )  # case-insensitive dedup -> no duplicate
    assert (
        await ws.add_allowed_domain(ws_row["id"], "github.com:443") is True
    )  # exact dup -> still True (idempotent)
    got = await ws.get_workspace_by_id(ws_row["id"])
    # Original entry preserved as-typed; new entry lowercased; no dup.
    assert got["allowed_domains"] == ["PyPI.org:443", "github.com:443"]


async def test_add_allowed_domain_from_empty(ws, user):
    # No allowed_domains yet (NULL) -> a single-element list.
    ws_row = await ws.create_workspace(user["id"], "fresh")
    assert await ws.add_allowed_domain(ws_row["id"], "example.com:443") is True
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] == ["example.com:443"]


async def test_add_allowed_domain_missing_workspace(ws):
    assert await ws.add_allowed_domain("nope", "example.com:443") is False


async def test_add_allowed_domain_rejects_malformed(ws, user):
    # A malformed entry is skipped (returns False), never raises -- the
    # verdict path must not break on persistence.
    ws_row = await ws.create_workspace(user["id"], "strict")
    assert await ws.add_allowed_domain(ws_row["id"], "not a domain!") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] is None


async def test_add_allowed_domain_ignores_blank(ws, user):
    # A blank entry normalizes to nothing (parse_allowed_domains drops it) ->
    # no append, returns False, never raises.
    ws_row = await ws.create_workspace(user["id"], "blank")
    assert await ws.add_allowed_domain(ws_row["id"], "   ") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] is None


async def test_add_rejected_domain_appends_lowercased_deduped(ws, user):
    # A forever consent deny persists by mutating rejected_domains (#2369) --
    # the mirror of add_allowed_domain. New entries lowercased + de-duplicated.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "forever-deny", rejected_domains=["PyPI.org:443"]
    )
    assert await ws.add_rejected_domain(ws_row["id"], "evil.com:443") is True
    assert (
        await ws.add_rejected_domain(ws_row["id"], "EVIL.COM:443") is True
    )  # case-insensitive dedup -> no duplicate
    assert (
        await ws.add_rejected_domain(ws_row["id"], "evil.com:443") is True
    )  # exact dup -> still True (idempotent)
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == ["PyPI.org:443", "evil.com:443"]


async def test_add_rejected_domain_from_empty(ws, user):
    ws_row = await ws.create_workspace(user["id"], "fresh-deny")
    assert await ws.add_rejected_domain(ws_row["id"], "evil.com:443") is True
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == ["evil.com:443"]


async def test_add_rejected_domain_missing_workspace(ws):
    assert await ws.add_rejected_domain("nope", "evil.com:443") is False


async def test_add_rejected_domain_rejects_malformed(ws, user):
    ws_row = await ws.create_workspace(user["id"], "strict-deny")
    assert await ws.add_rejected_domain(ws_row["id"], "not a domain!") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] is None


async def test_add_rejected_domain_ignores_blank(ws, user):
    ws_row = await ws.create_workspace(user["id"], "blank-deny")
    assert await ws.add_rejected_domain(ws_row["id"], "   ") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] is None


async def test_remove_allowed_domain_removes_case_insensitive(ws, user):
    # The inverse of add (#2370): revoking a forever allow retracts the entry.
    # Case-insensitive match; only the matched entry is removed.
    ws_row = await ws.create_workspace_with_acl(
        user["id"],
        "retract-allow",
        allowed_domains=["PyPI.org:443", "github.com:443"],
    )
    assert (
        await ws.remove_allowed_domain(ws_row["id"], "GITHUB.COM:443") is True
    )  # case-insensitive
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] == ["PyPI.org:443"]


async def test_remove_allowed_domain_idempotent_absent(ws, user):
    # Removing an absent entry is a no-op success (mirrors add's idempotency):
    # the desired post-state (entry absent) already holds.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "retract-absent", allowed_domains=["example.com:443"]
    )
    assert (
        await ws.remove_allowed_domain(ws_row["id"], "other.com:443") is True
    )
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] == ["example.com:443"]  # unchanged


async def test_remove_allowed_domain_from_null(ws, user):
    # NULL allowed_domains (never set) -> absent -> True, still NULL.
    ws_row = await ws.create_workspace(user["id"], "retract-null")
    assert (
        await ws.remove_allowed_domain(ws_row["id"], "example.com:443") is True
    )
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] is None


async def test_remove_allowed_domain_missing_workspace(ws):
    assert await ws.remove_allowed_domain("nope", "example.com:443") is False


async def test_remove_allowed_domain_rejects_malformed(ws, user):
    ws_row = await ws.create_workspace(user["id"], "retract-bad")
    assert (
        await ws.remove_allowed_domain(ws_row["id"], "not a domain!") is False
    )


async def test_remove_rejected_domain_removes_case_insensitive(ws, user):
    # The deny mirror (#2370): revoking a forever deny retracts the entry.
    ws_row = await ws.create_workspace_with_acl(
        user["id"],
        "retract-deny",
        rejected_domains=["Evil.com:443", "bad.com"],
    )
    assert (
        await ws.remove_rejected_domain(ws_row["id"], "evil.COM:443") is True
    )
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == ["bad.com"]


async def test_remove_rejected_domain_bare_host(ws, user):
    # A port-less deny was persisted as a bare host; retract the bare host.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "retract-bare", rejected_domains=["bad.com"]
    )
    assert await ws.remove_rejected_domain(ws_row["id"], "bad.com") is True
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == []


async def test_remove_rejected_domain_missing_workspace(ws):
    assert await ws.remove_rejected_domain("nope", "bad.com") is False


async def test_remove_rejected_domain_rejects_malformed(ws, user):
    ws_row = await ws.create_workspace(user["id"], "retract-deny-bad")
    assert (
        await ws.remove_rejected_domain(ws_row["id"], "not a domain!") is False
    )


async def test_remove_allowed_domain_ignores_blank(ws, user):
    # A blank entry normalizes to nothing -> no removal, returns False.
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "retract-blank", allowed_domains=["example.com:443"]
    )
    assert await ws.remove_allowed_domain(ws_row["id"], "   ") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["allowed_domains"] == ["example.com:443"]  # unchanged


async def test_remove_rejected_domain_ignores_blank(ws, user):
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "retract-deny-blank", rejected_domains=["bad.com"]
    )
    assert await ws.remove_rejected_domain(ws_row["id"], "   ") is False
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == ["bad.com"]  # unchanged


async def test_remove_rejected_domain_idempotent_absent(ws, user):
    # Removing an absent entry is a no-op success (mirrors allow's idempotency).
    ws_row = await ws.create_workspace_with_acl(
        user["id"], "retract-deny-absent", rejected_domains=["bad.com"]
    )
    assert (
        await ws.remove_rejected_domain(ws_row["id"], "other.com:443") is True
    )
    got = await ws.get_workspace_by_id(ws_row["id"])
    assert got["rejected_domains"] == ["bad.com"]  # unchanged


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


async def test_get_user_workspace_ids_includes_never_started(ws, user):
    """#2525: the admission quota counts in-flight starts, whose
    container_id is only persisted after podman create — so the id
    listing must NOT prefilter on container_id."""
    fresh = await ws.create_workspace(user["id"], "never-started")
    started = await ws.create_workspace(user["id"], "started")
    await ws.update_workspace_container(started["id"], "cid-y")
    ids = await ws.get_user_workspace_ids(user["id"])
    assert set(ids) == {fresh["id"], started["id"]}
    assert await ws.get_user_workspace_ids("nobody") == []


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


async def test_role_groups_carry_workspace_role_source(ws, app_state, user):
    """#2750: seeded role groups are marked ``workspace-role`` and their
    descriptions carry the normalized purpose."""
    from klangk.model.users import GROUP_SOURCE_WORKSPACE_ROLE

    ws_row = await ws.create_workspace_with_acl(user["id"], "marked-ws")
    for suffix in ("owners", "coders", "collaborators", "spectators"):
        group = await app_state.state.model.users.get_group_by_name(
            f"{suffix}-{ws_row['id']}"
        )
        assert group is not None
        assert group["source"] == GROUP_SOURCE_WORKSPACE_ROLE
        assert group["description"] == (
            f"Workspace role group: {suffix} of workspace marked-ws"
        )


async def test_delete_workspace_tears_down_unknown_suffix_role_group(
    ws, app_state, user
):
    """Teardown matches role groups by the source marker + workspace id —
    no role-suffix list — so a group seeded with a suffix outside the
    current four still tears down (#2750)."""
    from klangk.model.users import GROUP_SOURCE_WORKSPACE_ROLE

    ws_row = await ws.create_workspace_with_acl(user["id"], "odd-suffix")
    async with app_state.state.db.transaction() as db:
        await db.execute(
            "INSERT INTO groups (id, name, description, source)"
            " VALUES (?, ?, ?, ?)",
            (
                "odd-role-group-id",
                f"testers-{ws_row['id']}",
                "stray future role",
                GROUP_SOURCE_WORKSPACE_ROLE,
            ),
        )
    assert await ws.delete_workspace(ws_row["id"], user["id"]) is True
    assert (
        await app_state.state.model.users.get_group_by_id("odd-role-group-id")
        is None
    )


async def test_transfer_finds_owners_group_by_marker(ws, app_state, user):
    """Ownership transfer finds the owners group via the source marker +
    workspace id (#2750) — not a duplicated naming convention."""
    new_owner = await app_state.state.model.users.create_user(
        "marker-new@x.com", "h"
    )
    ws_row = await ws.create_workspace_with_acl(user["id"], "marker-transfer")
    owners = await app_state.state.model.users.get_group_by_name(
        f"owners-{ws_row['id']}"
    )
    transferred = await ws.transfer_workspace(ws_row["id"], new_owner["id"])
    assert transferred["user_id"] == new_owner["id"]
    members = await app_state.state.model.users.get_group_members(owners["id"])
    assert [m["id"] for m in members] == [new_owner["id"]]


async def test_add_acl_entry_rejects_cross_workspace_role_group(
    ws, app_state, user
):
    """#2750: a workspace-role group is grantable only on its own
    workspace's resource — model choke point raises for anything else."""
    from klangk.model import PRINCIPAL_GROUP, WorkspaceRoleScopeError

    ws_a = await ws.create_workspace_with_acl(user["id"], "guard-a")
    ws_b = await ws.create_workspace_with_acl(user["id"], "guard-b")
    owners_b = await app_state.state.model.users.get_group_by_name(
        f"owners-{ws_b['id']}"
    )
    with pytest.raises(WorkspaceRoleScopeError, match="grantable only"):
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_a['id']}",
            100,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_GROUP,
            group_id=owners_b["id"],
        )
    # Non-workspace resources are rejected too.
    with pytest.raises(WorkspaceRoleScopeError):
        await app_state.state.model.acl.add_acl_entry(
            "/",
            0,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_GROUP,
            group_id=owners_b["id"],
        )
    # Its own workspace's resource is fine.
    await app_state.state.model.acl.add_acl_entry(
        f"/workspaces/{ws_b['id']}",
        100,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_GROUP,
        group_id=owners_b["id"],
    )


async def test_replace_acl_entries_rejects_cross_workspace_role_group(
    ws, app_state, user
):
    """The replace writer carries the same guard (#2750)."""
    from klangk.model import (
        ACTION_DENY,
        PRINCIPAL_GROUP,
        PRINCIPAL_SYSTEM,
        SYSTEM_EVERYONE,
        WorkspaceRoleScopeError,
    )

    ws_a = await ws.create_workspace_with_acl(user["id"], "guard-replace-a")
    ws_b = await ws.create_workspace_with_acl(user["id"], "guard-replace-b")
    owners_b = await app_state.state.model.users.get_group_by_name(
        f"owners-{ws_b['id']}"
    )
    with pytest.raises(WorkspaceRoleScopeError):
        await app_state.state.model.acl.replace_acl_entries(
            f"/workspaces/{ws_a['id']}",
            [
                {
                    "position": 0,
                    "action": ACTION_ALLOW,
                    "principal_type": PRINCIPAL_GROUP,
                    "permission": "view",
                    "group_id": owners_b["id"],
                },
                {
                    "position": 1,
                    "action": ACTION_DENY,
                    "principal_type": PRINCIPAL_SYSTEM,
                    "permission": "*",
                    "system_principal": SYSTEM_EVERYONE,
                },
            ],
        )
    # Nothing was written (the whole replace rolls back).
    entries = await app_state.state.model.acl.get_acl_entries(
        f"/workspaces/{ws_a['id']}"
    )
    assert all(e["group_id"] != owners_b["id"] for e in entries)
