"""Direct coverage for ``UsersModel(app_state)`` (#1573).

Exercises every method on ``app_state.state.model.users`` — the app_state-owned
form app code is migrating to — plus the db-param helpers and the
agent-user cache. Mirrors the #1572 ``test_model_app_state.py`` pattern:
``app_state`` (db + model wired via the ContextVar DB) with the schema
initialized.
"""

import pytest

from klangk.model.users import (
    AGENT_USER_ID,
    AgentPrincipalError,
)


@pytest.fixture
async def users(app_state, db):
    """``app_state.state.model.users`` with the schema initialized."""
    return app_state.state.model.users


async def test_create_and_get_user(users):
    u = await users.create_user("a@x.com", "hash", verified=True)
    assert u["email"] == "a@x.com"
    assert u["verified"] is True
    by_email = await users.get_user_by_email("a@x.com")
    assert by_email["id"] == u["id"]
    by_id = await users.get_user_by_id(u["id"])
    assert by_id["handle"] == u["handle"]
    # Never logged in: the column exists and reads NULL (#2583).
    assert by_id["last_login_at"] is None
    assert await users.get_user_by_id("nope") is None
    assert await users.get_user_by_email("missing@x.com") is None


async def test_record_login(users):
    """record_login stamps a UTC ISO timestamp readable via
    get_user_by_id (#2583)."""
    from datetime import datetime

    u = await users.create_user("login@x.com", "hash", verified=True)
    await users.record_login(u["id"])
    by_id = await users.get_user_by_id(u["id"])
    stamped = by_id["last_login_at"]
    assert stamped is not None
    # Round-trips as a timezone-aware ISO-8601 timestamp.
    dt = datetime.fromisoformat(stamped)
    assert dt.tzinfo is not None


async def test_get_user_by_handle_and_handle(users):
    u = await users.create_user("b@x.com", "hash")
    assert await users.get_user_handle(u["id"]) == u["handle"]
    by_handle = await users.get_user_by_handle(u["handle"])
    assert by_handle["id"] == u["id"]
    assert await users.get_user_by_handle("nope") is None
    assert await users.get_user_handle("nope") is None


async def test_get_user_by_identifier(users):
    """get_user_by_identifier resolves by email (contains '@') or by
    handle (no '@'), returning the full row incl. password_hash (#616)."""
    u = await users.create_user("ident@x.com", "secret-hash", verified=True)
    handle = u["handle"]
    # email branch
    by_email = await users.get_user_by_identifier("ident@x.com")
    assert by_email["id"] == u["id"]
    # handle branch — same full-row shape as get_user_by_email
    by_handle = await users.get_user_by_identifier(handle)
    assert by_handle["id"] == u["id"]
    assert by_handle["email"] == "ident@x.com"
    assert by_handle["password_hash"] == "secret-hash"
    assert by_handle["handle"] == handle
    assert by_handle["verified"] is True
    # missing in each namespace
    assert await users.get_user_by_identifier("nope@x.com") is None
    assert await users.get_user_by_identifier("nope-handle") is None


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
    async with users.app.state.db.transaction() as db:
        handle = await users.insert_unverified_user(
            db, "uid-uv", "uv@x.com", "hash"
        )
    assert handle
    fetched = await users.get_user_by_email("uv@x.com")
    assert fetched["id"] == "uid-uv"
    assert fetched["verified"] is False


async def test_create_group_and_lookup(users):
    g = await users.create_group("g1", description="d")
    assert g["source"] == "manual"
    by_name = await users.get_group_by_name("g1")
    assert by_name["id"] == g["id"]
    assert by_name["source"] == "manual"
    by_id = await users.get_group_by_id(g["id"])
    assert by_id["name"] == "g1"
    assert by_id["source"] == "manual"
    assert await users.get_group_by_name("missing") is None
    assert await users.get_group_by_id("missing") is None


async def test_create_group_rejects_unknown_source(users):
    from klangk.model.users import GROUP_SOURCES

    with pytest.raises(ValueError, match="Invalid group source"):
        await users.create_group("bad-source", source="nope")
    assert "nope" not in GROUP_SOURCES


async def test_create_group_with_workspace_role_source(users):
    from klangk.model.users import GROUP_SOURCE_WORKSPACE_ROLE

    g = await users.create_group("wr-1", source=GROUP_SOURCE_WORKSPACE_ROLE)
    fetched = await users.get_group_by_id(g["id"])
    assert fetched["source"] == GROUP_SOURCE_WORKSPACE_ROLE


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


async def test_list_groups_source_filter(users, app_state):
    """#2750: source filter hides/shows workspace-role groups; rows carry
    the source marker; the default shows everything."""
    from klangk.model.users import (
        GROUP_SOURCE_MANUAL,
        GROUP_SOURCE_WORKSPACE_ROLE,
    )

    manual = await users.create_group("manual-g")
    seeded = await users.create_group(
        "owners-deadbeef", source=GROUP_SOURCE_WORKSPACE_ROLE
    )
    all_rows = await users.list_groups()
    assert {manual["id"], seeded["id"]} <= {
        gr["id"] for gr in all_rows["groups"]
    }
    assert all("source" in gr for gr in all_rows["groups"])

    only_manual = await users.list_groups(source=GROUP_SOURCE_MANUAL)
    ids = {gr["id"] for gr in only_manual["groups"]}
    assert manual["id"] in ids
    assert seeded["id"] not in ids
    assert only_manual["total"] == len(ids)

    only_roles = await users.list_groups(source=GROUP_SOURCE_WORKSPACE_ROLE)
    assert [gr["id"] for gr in only_roles["groups"]] == [seeded["id"]]

    # q + source compose.
    both = await users.list_groups(
        q="manual-g", source=GROUP_SOURCE_WORKSPACE_ROLE
    )
    assert both["total"] == 0
    assert both["groups"] == []


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


async def test_search_users_matches_handle_prefix(users):
    """search_users matches an email *or* handle prefix (#616)."""
    u = await users.create_user("findme@x.com", "hash")
    handle = u["handle"]
    # full handle + its prefix both hit
    assert any(f["id"] == u["id"] for f in await users.search_users(handle))
    assert any(
        f["id"] == u["id"] for f in await users.search_users(handle[:3])
    )
    # still matches an email prefix too
    assert any(f["id"] == u["id"] for f in await users.search_users("findme@"))


async def test_update_email_and_password(users):
    u = await users.create_user("u@x.com", "hash")
    await users.update_email(u["id"], "u2@x.com")
    assert (await users.get_user_by_id(u["id"]))["email"] == "u2@x.com"
    await users.update_password(u["id"], "newhash")
    fetched = await users.get_user_by_email("u2@x.com")
    assert fetched["password_hash"] == "newhash"


async def test_mark_unverified(users):
    u = await users.create_user("unv@x.com", "hash", verified=True)
    assert (await users.get_user_by_email("unv@x.com"))["verified"] is True
    await users.mark_unverified(u["id"])
    assert (await users.get_user_by_email("unv@x.com"))["verified"] is False


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


async def test_ensure_agent_user_seeds_and_upserts(users, app_state):
    """ensure_agent_user creates the fixed-identity row and reconciles
    a drifted (pre-#2718) row back to it (#3068)."""
    await users.ensure_agent_user()
    agent = await users.get_user_by_id(AGENT_USER_ID)
    assert agent["email"] == "klangk@example.com"
    assert agent["handle"] == "klangk"
    # Drift the row (clanker-era), then re-seed: reconciled.
    async with app_state.state.db.transaction() as db:
        await db.execute(
            "UPDATE users SET handle = ?, email = ? WHERE id = ?",
            ("clanker", "clanker@example.com", AGENT_USER_ID),
        )
    await users.ensure_agent_user()
    agent = await users.get_user_by_id(AGENT_USER_ID)
    assert agent["handle"] == "klangk"
    assert agent["email"] == "klangk@example.com"


async def test_ensure_agent_user_refuses_human_handle_collision(
    users, app_state
):
    """A human holding the fixed handle aborts the seed (#1137, #3068)."""
    human = await users.create_user("alice@example.com", "hash", verified=True)
    async with app_state.state.db.transaction() as db:
        await db.execute(
            "UPDATE users SET handle = 'klangk' WHERE id = ?",
            (human["id"],),
        )
    with pytest.raises(RuntimeError, match="klangk"):
        await users.ensure_agent_user()
    assert await users.get_user_by_id(AGENT_USER_ID) is None
    # The colliding human is untouched by the refusal.
    assert (await users.get_user_by_handle("klangk"))["id"] == human["id"]


async def test_agent_handle_reserved(users, agent_user):
    """The agent's handle is statically reserved (#2718): no human can
    take it, seeded or not."""
    users.clear_agent_cache()
    u = await users.create_user("ag@x.com", "hash")
    with pytest.raises(ValueError, match="is reserved"):
        await users.set_user_handle(u["id"], "klangk")
    users.clear_agent_cache()


async def test_db_param_handle_helpers(users):
    u = await users.create_user("h@x.com", "hash")
    base = (await users.get_user_handle(u["id"])) or "handle"
    async with users.app.state.db.transaction() as db:
        uniq = await users.unique_handle(db, base)
        gen = await users.generate_handle(db, "new@email.com")
    assert uniq  # base taken by the user -> suffixed or hashed
    assert gen


async def test_backfill_handles_method(users):
    async with users.app.state.db.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified, handle)"
            " VALUES (?, ?, ?, 0, NULL)",
            ("bf-id", "bf@x.com", "h"),
        )
        await users.backfill_handles(db)
    fetched = await users.get_user_by_id("bf-id")
    assert fetched["handle"] is not None


async def test_unique_handle_truncates_long_suffix(users):
    """A base near MAX_HANDLE_LEN gets its numeric suffix truncated."""
    from klangk.model import MAX_HANDLE_LEN

    long = "a" * MAX_HANDLE_LEN
    async with users.app.state.db.transaction() as db:
        await db.execute(
            "INSERT INTO users (id, email, password_hash, verified, handle)"
            " VALUES (?, ?, ?, 0, ?)",
            ("long-id", "long@x.com", "h", long),
        )
        result = await users.unique_handle(db, long)
    assert len(result) <= MAX_HANDLE_LEN
    assert result.endswith("-2")


async def test_unique_handle_falls_back_to_hash(users):
    """When every numeric suffix collides, fall back to a hashed handle."""
    from unittest.mock import AsyncMock

    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=(1,))
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_cursor)

    result = await users.unique_handle(mock_db, "taken")
    # hash_fallback_handle: "<base>-<sha256[:8]>"
    assert "-" in result
    assert len(result.rsplit("-", 1)[1]) == 8


# --- inactivity tracking + dormant-account sweep (#2588) ---


async def _set_ts(users, user_id, column, dt):
    """Overwrite a users-table timestamp column for a test."""
    async with users.app.state.db.transaction() as db:
        await db.execute(
            f"UPDATE users SET {column} = ? WHERE id = ?",  # noqa: S608
            (dt, user_id),
        )


async def test_new_users_are_enabled(users):
    """The migration default keeps accounts enabled until the sweep or an
    admin says otherwise (#2588)."""
    u = await users.create_user("fresh@x.com", "hash", verified=True)
    by_id = await users.get_user_by_id(u["id"])
    by_email = await users.get_user_by_email(u["email"])
    assert by_id["disabled"] is False
    assert by_email["disabled"] is False
    assert by_id["last_activity_at"] is None


async def test_record_activity(users):
    """record_activity stamps a UTC ISO timestamp readable via
    get_user_by_id (#2588)."""
    from datetime import datetime

    u = await users.create_user("active@x.com", "hash", verified=True)
    await users.record_activity(u["id"])
    by_id = await users.get_user_by_id(u["id"])
    dt = datetime.fromisoformat(by_id["last_activity_at"])
    assert dt.tzinfo is not None


async def test_set_user_disabled_roundtrip(users):
    u = await users.create_user("togglable@x.com", "hash", verified=True)
    assert await users.set_user_disabled(u["id"], True) is True
    assert (await users.get_user_by_id(u["id"]))["disabled"] is True
    assert await users.set_user_disabled(u["id"], False) is True
    assert (await users.get_user_by_id(u["id"]))["disabled"] is False
    assert await users.set_user_disabled("nope", True) is False


async def test_set_user_disabled_rejects_agent(users):
    with pytest.raises(AgentPrincipalError, match="system agent"):
        await users.set_user_disabled(AGENT_USER_ID, True)


async def test_disable_inactive_users_by_window(users):
    """Accounts older than the window are disabled; fresh ones are not."""
    from datetime import datetime, timedelta, timezone

    old = await users.create_user("old@x.com", "hash", verified=True)
    fresh = await users.create_user("fresh2@x.com", "hash", verified=True)
    stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    await _set_ts(users, old["id"], "last_activity_at", stale)
    await _set_ts(users, old["id"], "created_at", stale)

    disabled = await users.disable_inactive_users(days=35)
    assert [d["email"] for d in disabled] == ["old@x.com"]
    assert (await users.get_user_by_id(old["id"]))["disabled"] is True
    assert (await users.get_user_by_id(fresh["id"]))["disabled"] is False


async def test_disable_inactive_users_newest_signal_wins(users):
    """A fresh login rescues an account whose last_activity_at is stale —
    the sweep judges on the newest of activity/login/creation (#2588)."""
    from datetime import datetime, timedelta, timezone

    u = await users.create_user("mixed@x.com", "hash", verified=True)
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    await _set_ts(users, u["id"], "last_activity_at", stale)
    await _set_ts(users, u["id"], "last_login_at", recent)

    disabled = await users.disable_inactive_users(days=35)
    assert u["email"] not in [d["email"] for d in disabled]
    assert (await users.get_user_by_id(u["id"]))["disabled"] is False


async def test_disable_inactive_users_exempts_admins_and_agent(users):
    """Admin-group members and the system agent are never auto-disabled —
    an idle deploy must not lock out every operator (#2588)."""
    from datetime import datetime, timedelta, timezone

    group = await users.create_group("admins")
    admin = await users.create_user("admin@x.com", "hash", verified=True)
    await users.add_user_to_group(admin["id"], group["id"])
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    await _set_ts(users, admin["id"], "last_activity_at", stale)
    await _set_ts(users, admin["id"], "created_at", stale)
    await _set_ts(users, AGENT_USER_ID, "last_activity_at", stale)
    await _set_ts(users, AGENT_USER_ID, "created_at", stale)

    disabled = await users.disable_inactive_users(days=35)
    emails = [d["email"] for d in disabled]
    assert "admin@x.com" not in emails
    assert AGENT_USER_ID not in [d["id"] for d in disabled]


async def test_disable_inactive_users_skips_already_disabled(users):
    """A second sweep does not re-report accounts it disabled."""
    from datetime import datetime, timedelta, timezone

    u = await users.create_user("twice@x.com", "hash", verified=True)
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    await _set_ts(users, u["id"], "last_activity_at", stale)
    await _set_ts(users, u["id"], "created_at", stale)
    first = await users.disable_inactive_users(days=35)
    second = await users.disable_inactive_users(days=35)
    assert [d["email"] for d in first] == ["twice@x.com"]
    assert second == []


async def test_disable_inactive_users_boundary_is_inclusive(users):
    """Activity just inside the window keeps the account; just past it
    (35 days + 1 hour) disables it. The exact boundary is not asserted —
    it is inclusive, but the sweep's clock runs microseconds after the
    test's."""
    from datetime import datetime, timedelta, timezone

    edge = await users.create_user("edge@x.com", "hash", verified=True)
    over = await users.create_user("over@x.com", "hash", verified=True)
    now = datetime.now(timezone.utc)
    inside = (now - timedelta(days=35) + timedelta(minutes=5)).isoformat()
    past = (now - timedelta(days=35, hours=1)).isoformat()
    for column in ("last_activity_at", "created_at"):
        await _set_ts(users, edge["id"], column, inside)
        await _set_ts(users, over["id"], column, past)
    disabled = await users.disable_inactive_users(days=35)
    emails = [d["email"] for d in disabled]
    assert "edge@x.com" not in emails
    assert "over@x.com" in emails


async def test_disable_inactive_users_zero_is_noop(users):
    """days=0 (the sweep disabled) disables nothing."""
    from datetime import datetime, timedelta, timezone

    u = await users.create_user("zero@x.com", "hash", verified=True)
    stale = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    await _set_ts(users, u["id"], "last_activity_at", stale)
    await _set_ts(users, u["id"], "created_at", stale)
    assert await users.disable_inactive_users(days=0) == []
    assert (await users.get_user_by_id(u["id"]))["disabled"] is False


async def test_disable_inactive_users_handles_legacy_naive_timestamps(users):
    """SQLite-format ('YYYY-MM-DD HH:MM:SS', naive UTC) timestamps parse
    and are judged — old rows use that format."""
    from datetime import datetime, timedelta, timezone

    u = await users.create_user("legacy@x.com", "hash", verified=True)
    naive = (datetime.now(timezone.utc) - timedelta(days=40)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    await _set_ts(users, u["id"], "last_login_at", naive)
    await _set_ts(users, u["id"], "created_at", naive)
    disabled = await users.disable_inactive_users(days=35)
    assert "legacy@x.com" in [d["email"] for d in disabled]


def test_parse_user_ts_variants():
    """parse_user_ts handles NULL, naive, aware, and garbage values."""
    from datetime import timezone

    from klangk.model.users import parse_user_ts

    assert parse_user_ts(None) is None
    assert parse_user_ts("") is None
    naive = parse_user_ts("2026-01-15 10:00:00")
    assert naive is not None and naive.tzinfo is timezone.utc
    aware = parse_user_ts("2026-01-15T10:00:00+02:00")
    assert aware is not None and aware.utcoffset().total_seconds() == 7200
    assert parse_user_ts("garbage") is None


async def test_create_user_returns_disabled_false(users):
    """create_user's dict carries the disabled key explicitly — the
    ensure_not_disabled gate must never silently pass on a missing
    key (#2588 review)."""
    u = await users.create_user("shape@x.com", "hash", verified=True)
    assert u["disabled"] is False


async def test_update_group_rejects_workspace_role_rename(
    users, user, app_state
):
    """#2750 review: renaming a workspace-role group would orphan it on
    teardown and misdirect the ACL scope guard (both parse the
    workspace-id suffix of the name) — refused at the model."""
    from klangk.model import WorkspaceRoleScopeError

    workspaces = app_state.state.model.workspaces
    ws_row = await workspaces.create_workspace_with_acl(
        user["id"], "no-rename"
    )
    role_group = await users.get_group_by_name(f"owners-{ws_row['id']}")
    with pytest.raises(WorkspaceRoleScopeError, match="cannot be changed"):
        await users.update_group(role_group["id"], name="renamed")
    # Description stays editable.
    assert (
        await users.update_group(
            role_group["id"], description="still editable"
        )
        is True
    )
    # Manual groups rename freely.
    manual = await users.create_group("free-to-rename")
    assert await users.update_group(manual["id"], name="renamed") is True


async def test_admins_group_identity_is_protected(
    users, app_state, admin_group
):
    """#2995: ``is_admin`` derives from a group *named* ``admins`` —
    renames onto/off the name and deletes are refused at the model
    choke points (a delegated group manager must not be able to strip
    every admin's status or mint a fake admins group)."""
    from klangk.model import AdminGroupProtectionError

    admin_group = await users.get_group_by_name("admins")
    assert admin_group is not None
    with pytest.raises(AdminGroupProtectionError, match="cannot be renamed"):
        await users.update_group(admin_group["id"], name="super")
    with pytest.raises(AdminGroupProtectionError, match="reserved"):
        await users.update_group(
            (await users.create_group("impostors"))["id"],
            name="admins",
        )
    with pytest.raises(AdminGroupProtectionError, match="cannot be deleted"):
        await users.delete_group(admin_group["id"])
    # Same-name no-op, description edits, and unknown ids pass through
    # the guard untouched.
    assert (
        await users.update_group(
            admin_group["id"], name="admins", description="operators"
        )
        is True
    )
    assert await users.update_group("no-such-id", name="x") is False
    assert await users.delete_group("no-such-id") is False


class TestUsersBranchGaps2834:
    async def test_backfill_no_handleless_rows_commits_nothing(
        self, user, app_state
    ):
        # Every user already has a handle: the model's backfill runs zero
        # UPDATEs and skips the (empty) commit entirely.
        handle_before = user["handle"]
        async with app_state.state.db.transaction() as tx:
            await app_state.state.model.users.backfill_handles(tx)
        refreshed = await app_state.state.model.users.get_user_by_email(
            user["email"]
        )
        assert refreshed["handle"] == handle_before
