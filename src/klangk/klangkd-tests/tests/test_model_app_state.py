"""Direct coverage for the ``Model(app_state)`` composition root (#1572).

The per-domain ``*Model`` classes (tokens, login_attempts, invitations, ports)
and the ``Model`` transaction/init helpers are exercised here through
``app_state_with_schema.state.model`` — the same surface app code is migrating to. The
module-level free-function backstops that lost their app-code callers (they
moved to the class methods) are covered here too.
"""

import pytest

from klangk import model


@pytest.fixture
async def app_state_with_schema(app_state, db):
    """app_state (db + model wired) with the schema initialized."""
    return app_state


class TestModelRoot:
    """The Model composition root's own helpers (transaction/fetchone/get_db/init_db)."""

    async def test_init_db_creates_schema(self, app_state, temp_data_dir):
        # init_db via the Model method against the app_state DB.
        await app_state.state.model.init_db()
        async with app_state.state.model.transaction() as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {r[0] for r in await cursor.fetchall()}
        assert "users" in tables
        assert "port_allocations" in tables

    async def test_transaction_commits_and_rolls_back(
        self, app_state_with_schema
    ):
        m = app_state_with_schema.state.model
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
        db = await app_state_with_schema.state.model.get_db()
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
        t = app_state_with_schema.state.model.tokens
        assert await t.is_token_blocklisted("nope") is False
        await t.blocklist_token("jti-1", "2099-01-01", new_token="new-jwt")
        assert await t.is_token_blocklisted("jti-1") is True
        assert await t.get_refreshed_token("jti-1") == "new-jwt"
        assert await t.get_refreshed_token("none") is None

    async def test_free_function_backstop(self, app_state_with_schema):
        # The ContextVar-backed free function still works (backstop, #1578).
        await app_state_with_schema.state.model.tokens.blocklist_token(
            "ff-jti", "2099-01-01", new_token="ff-new"
        )
        assert (
            await app_state_with_schema.state.model.tokens.get_refreshed_token(
                "ff-jti"
            )
            == "ff-new"
        )
        assert (
            await app_state_with_schema.state.model.tokens.is_token_blocklisted(
                "ff-jti"
            )
            is True
        )


class TestLoginAttemptsModel:
    async def test_record_and_lock_and_clear(self, app_state_with_schema):
        la = app_state_with_schema.state.model.login_attempts
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
        la = app_state_with_schema.state.model.login_attempts
        await la.set_login_lockout("r@b.com", "2099-01-01")
        await la.record_failed_login("r@b.com", reset=True)
        info = await la.get_login_attempt_info("r@b.com")
        assert info["attempt_count"] == 1
        assert info["locked_until"] is None


class TestInvitationsModel:
    async def test_create_get_list_revoke(self, app_state_with_schema, user):
        inv = app_state_with_schema.state.model.invitations
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
        inv = app_state_with_schema.state.model.invitations
        created = await inv.create_invitation("z@y.com", user["id"])
        assert await inv.mark_invitation_accepted(created["id"]) is True
        assert await inv.mark_invitation_accepted(created["id"]) is False

    async def test_free_function_backstop_list_with_q(
        self, app_state_with_schema, user
    ):
        await app_state_with_schema.state.model.invitations.create_invitation(
            "q@y.com", user["id"]
        )
        listed = await app_state_with_schema.state.model.invitations.list_invitations(
            q="q@y"
        )
        assert listed["total"] == 1


class TestPortsModel:
    async def test_add_find_remove_get(self, app_state_with_schema, workspace):
        p = app_state_with_schema.state.model.ports
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


class TestACLModel:
    """Direct coverage for the ``ACLModel`` class methods (#1574).

    The method bodies mirror the module-level free functions (backstop);
    both copies are kept until #1578, so both need coverage. The backstop
    is covered indirectly by ``klangkd/acl.py`` (still on the
    ContextVar path) and directly in ``test_model.py``; this class covers
    the class methods through ``app_state_with_schema.state.model.acl`` — the surface the
    API routes and the seed now use.
    """

    async def test_add_get_replace_delete_resolved(
        self, app_state_with_schema, user
    ):
        a = app_state_with_schema.state.model.acl
        resource = "/workspaces/acl-cls"
        eid = await a.add_acl_entry(
            resource,
            0,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        assert isinstance(eid, int)
        entries = await a.get_acl_entries(resource)
        assert len(entries) == 1 and entries[0]["id"] == eid

        # get_acl_entries_map (class method — no production caller yet;
        # ``acl.py`` still uses the backstop).
        amap = await a.get_acl_entries_map([resource, "/none/here"])
        assert len(amap[resource]) == 1
        assert amap["/none/here"] == []
        assert await a.get_acl_entries_map([]) == {}

        # resolved view carries the user email as the principal name
        resolved = await a.get_acl_entries_resolved(resource)
        assert resolved[0]["principal"] == user["email"]

        # replace (with a system-everyone deny) then read back
        await a.replace_acl_entries(
            resource,
            [
                {
                    "position": 0,
                    "action": model.ACTION_DENY,
                    "principal_type": model.PRINCIPAL_SYSTEM,
                    "system_principal": model.SYSTEM_EVERYONE,
                    "permission": "*",
                    "user_id": None,
                    "group_id": None,
                }
            ],
        )
        after = await a.get_acl_entries(resource)
        assert len(after) == 1
        assert after[0]["action"] == model.ACTION_DENY

        # delete returns the count, then the resource is empty
        deleted = await a.delete_acl_entries_for_resource(resource)
        assert deleted == 1
        assert await a.get_acl_entries(resource) == []

    async def test_by_principal_user_and_group(
        self, app_state_with_schema, user
    ):
        a = app_state_with_schema.state.model.acl
        group = await app_state_with_schema.state.model.users.create_group(
            "acl-group"
        )
        await a.add_acl_entry(
            "/by-princ",
            0,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        await a.add_acl_entry(
            "/by-princ",
            1,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_GROUP,
            group_id=group["id"],
        )
        by_user = await a.get_acl_entries_by_principal_user(user["id"])
        assert len(by_user) == 1
        by_group = await a.get_acl_entries_by_principal_group(group["id"])
        assert len(by_group) == 1

    async def test_tree_summary(self, app_state_with_schema, user):
        a = app_state_with_schema.state.model.acl
        await a.add_acl_entry(
            "/tree/one",
            0,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_EVERYONE,
        )
        tree = await a.get_acl_tree_summary()
        resources = {row["resource"] for row in tree}
        assert "/tree/one" in resources

    async def test_add_rejects_agent_principal(self, app_state_with_schema):
        from klangk.model import AgentPrincipalError

        a = app_state_with_schema.state.model.acl
        with pytest.raises(AgentPrincipalError):
            await a.add_acl_entry(
                "/agent-guard",
                0,
                model.ACTION_ALLOW,
                "view",
                model.PRINCIPAL_USER,
                user_id=model.AGENT_USER_ID,
            )

    async def test_replace_rejects_agent_principal(
        self, app_state_with_schema
    ):
        from klangk.model import AgentPrincipalError

        a = app_state_with_schema.state.model.acl
        with pytest.raises(AgentPrincipalError):
            await a.replace_acl_entries(
                "/agent-guard",
                [
                    {
                        "position": 0,
                        "action": model.ACTION_ALLOW,
                        "principal_type": model.PRINCIPAL_USER,
                        "user_id": model.AGENT_USER_ID,
                        "group_id": None,
                        "system_principal": None,
                        "permission": "view",
                    }
                ],
            )


class TestNoConfigDivergenceRegression:
    """#1551 / #1578: the DB a request/seed path uses is the one the app was
    built with (``app_state.state.db``), never an env-only lazy fallback.

    Pre-#1578, ``model.db.get_current_db()`` lazily built
    ``DB(KlangkSettings(os.environ))`` when nothing was bound — so a process
    started from a config file whose ``data_dir`` differed from ambient
    ``KLANGK_DATA_DIR`` would read/write a *different* SQLite file than the
    one ``init_db`` had populated. With the ContextVar + delegates gone, every
    path reaches ``app_state.state.db``; this test asserts the divergence is
    structurally impossible.
    """

    def test_no_env_only_db_construction_path_exists(self):
        """The ContextVar, its binders, and the module-level DB delegates that
        hid the env-only fallback are gone from model.db."""
        import klangk.model.db as db_mod

        for gone in (
            "set_current_db",
            "reset_current_db",
            "get_current_db",
        ):
            assert not hasattr(db_mod, gone), (
                f"model.db.{gone} must be gone (it was the #1551 divergence path)"
            )
        # No ambient/lazy DB delegate on the package either.
        assert not hasattr(model, "get_current_db")
        assert not hasattr(model, "transaction")
        assert not hasattr(model, "fetchone")

    async def test_db_follows_settings_not_env(self, tmp_path, monkeypatch):
        """init_db + a model write both land on the config-file data_dir,
        never on the ambient ``KLANGK_DATA_DIR`` dir."""
        import types

        from _helpers import make_settings

        config_data = tmp_path / "d"
        ambient_data = tmp_path / "amb"
        config_data.mkdir()
        ambient_data.mkdir()

        # Ambient env points at a DIFFERENT data dir than the app's settings.
        monkeypatch.setenv("KLANGK_DATA_DIR", str(ambient_data))
        monkeypatch.setenv("KLANGK_STATE_DIR", str(tmp_path / "as"))

        # The app is built from settings whose data_dir is the configured one
        # (mirrors a config-file-launched process). Build the owned DB + Model
        # directly from those settings — NOT via wire_db_and_model, which
        # reuses the shared per-test DB and would mask the divergence.
        from klangk.model import Model
        from klangk.model.db import DB

        settings = make_settings(
            {
                "KLANGK_DATA_DIR": str(config_data),
                "KLANGK_STATE_DIR": str(tmp_path / "cs"),
            }
        )
        state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        state.state.db = DB(state)
        state.state.model = Model(state)

        # The owned DB resolves to the configured path, not the ambient one.
        assert state.state.db.db_path.parent == config_data
        assert str(state.state.db.db_path).startswith(str(config_data))

        await state.state.model.init_db()
        await state.state.model.users.create_user(
            "divergence@example.com", "hash", verified=True
        )

        # Exactly one klangk.db exists, under the configured data_dir.
        assert (config_data / "klangk.db").exists()
        # Nothing was written under the ambient env data_dir.
        assert not list(ambient_data.glob("*.db"))
