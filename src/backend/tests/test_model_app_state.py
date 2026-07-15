"""Direct coverage for the ``Model(app_state)`` composition root (#1572).

The per-domain ``*Model`` classes (tokens, login_attempts, invitations, ports)
and the ``Model`` transaction/init helpers are exercised here through
``app_state.model`` — the same surface app code is migrating to. The
module-level free-function backstops that lost their app-code callers (they
moved to the class methods) are covered here too.
"""

import pytest

from klangk_backend import model


@pytest.fixture
async def app_state_with_schema(app_state, db):
    """app_state (db + model wired) with the schema initialized."""
    return app_state


class TestModelRoot:
    """The Model composition root's own helpers (transaction/fetchone/get_db/init_db)."""

    async def test_init_db_creates_schema(self, app_state, temp_data_dir):
        # init_db via the Model method against the app_state DB.
        await app_state.model.init_db()
        async with app_state.model.transaction() as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {r[0] for r in await cursor.fetchall()}
        assert "users" in tables
        assert "port_allocations" in tables

    async def test_transaction_commits_and_rolls_back(
        self, app_state_with_schema
    ):
        m = app_state_with_schema.model
        async with m.transaction() as db:
            await db.execute(
                "INSERT INTO token_blocklist (jti, expires_at) VALUES (?, ?)",
                ("root-jti", "2099-01-01"),
            )
        # committed: visible via fetchone
        row = await m.fetchone(
            "SELECT jti FROM token_blocklist WHERE jti = ?", ("root-jti",)
        )
        assert row is not None and row["jti"] == "root-jti"

        # rollback path: an exception inside the block discards the write
        with pytest.raises(RuntimeError):
            async with m.transaction() as db:
                await db.execute(
                    "INSERT INTO token_blocklist (jti, expires_at)"
                    " VALUES (?, ?)",
                    ("rolled-jti", "2099-01-01"),
                )
                raise RuntimeError("boom")
        row = await m.fetchone(
            "SELECT jti FROM token_blocklist WHERE jti = ?", ("rolled-jti",)
        )
        assert row is None

    async def test_get_db_returns_raw_connection(self, app_state_with_schema):
        db = await app_state_with_schema.model.get_db()
        try:
            await db.execute(
                "INSERT INTO token_blocklist (jti, expires_at) VALUES (?, ?)",
                ("raw-jti", "2099-01-01"),
            )
            await db.commit()
        finally:
            await db.close()


class TestTokensModel:
    async def test_blocklist_and_check_and_refresh(
        self, app_state_with_schema
    ):
        t = app_state_with_schema.model.tokens
        assert await t.is_token_blocklisted("nope") is False
        await t.blocklist_token("jti-1", "2099-01-01", new_token="new-jwt")
        assert await t.is_token_blocklisted("jti-1") is True
        assert await t.get_refreshed_token("jti-1") == "new-jwt"
        assert await t.get_refreshed_token("none") is None

    async def test_free_function_backstop(self, app_state_with_schema):
        # The ContextVar-backed free function still works (backstop, #1578).
        await model.blocklist_token("ff-jti", "2099-01-01", new_token="ff-new")
        assert await model.get_refreshed_token("ff-jti") == "ff-new"
        assert await model.is_token_blocklisted("ff-jti") is True


class TestLoginAttemptsModel:
    async def test_record_and_lock_and_clear(self, app_state_with_schema):
        la = app_state_with_schema.model.login_attempts
        await la.record_failed_login("a@b.com")
        info = await la.get_login_attempt_info("a@b.com")
        assert info["attempt_count"] == 1
        await la.record_failed_login("a@b.com")
        assert (await la.get_login_attempt_info("a@b.com"))[
            "attempt_count"
        ] == 2
        await la.set_login_lockout("a@b.com", "2099-01-01")
        assert (await la.get_login_attempt_info("a@b.com"))[
            "locked_until"
        ] == "2099-01-01"
        await la.clear_login_attempts("a@b.com")
        assert await la.get_login_attempt_info("a@b.com") is None

    async def test_record_reset_clears_lockout(self, app_state_with_schema):
        la = app_state_with_schema.model.login_attempts
        await la.set_login_lockout("r@b.com", "2099-01-01")
        await la.record_failed_login("r@b.com", reset=True)
        info = await la.get_login_attempt_info("r@b.com")
        assert info["attempt_count"] == 1
        assert info["locked_until"] is None


class TestInvitationsModel:
    async def test_create_get_list_revoke(self, app_state_with_schema, user):
        inv = app_state_with_schema.model.invitations
        created = await inv.create_invitation("x@y.com", user["id"])
        got = await inv.get_invitation(created["id"])
        assert got["email"] == "x@y.com"
        assert await inv.get_invitation("missing") is None
        assert await inv.get_pending_invitation_by_email("x@y.com") is not None
        listed = await inv.list_invitations(q="x@y")
        assert listed["total"] == 1
        assert listed["pending_count"] == 1
        assert await inv.revoke_invitation(created["id"]) is True
        assert await inv.revoke_invitation(created["id"]) is False

    async def test_mark_accepted(self, app_state_with_schema, user):
        inv = app_state_with_schema.model.invitations
        created = await inv.create_invitation("z@y.com", user["id"])
        assert await inv.mark_invitation_accepted(created["id"]) is True
        assert await inv.mark_invitation_accepted(created["id"]) is False

    async def test_free_function_backstop_list_with_q(
        self, app_state_with_schema, user
    ):
        await model.create_invitation("q@y.com", user["id"])
        listed = await model.list_invitations(q="q@y")
        assert listed["total"] == 1


class TestPortsModel:
    async def test_add_find_remove_get(self, app_state_with_schema, workspace):
        p = app_state_with_schema.model.ports
        ws_id = workspace["id"]
        await p.add_port_allocations(ws_id, [9000, 9001])
        assert await p.get_workspace_ports(ws_id) == [9000, 9001]
        # find_and_allocate skips already-allocated 9000/9001
        new = await p.find_and_allocate_ports(ws_id, 1, 9000)
        assert 9000 not in new and 9001 not in new
        await p.remove_port_allocations(ws_id, [9000])
        assert 9000 not in await p.get_workspace_ports(ws_id)
        all_ports = await p.get_all_allocated_ports()
        assert 9001 in all_ports
