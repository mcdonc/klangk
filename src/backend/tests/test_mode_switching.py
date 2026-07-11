"""End-to-end acceptance tests for switching ``KLANGK_AUTH_MODES``.

These are deliberately **high-level**: each test drives the real HTTP API
(starting in one auth mode, performing the documented upgrade/downgrade
steps, then flipping the mode) and asserts the behaviour the
*Auth Modes* guide promises a real operator. They exist precisely to keep
the docs honest — if a documented flow stops working, these tests fail.

Mode is read live from the environment on every request
(``oidc.auth_modes(settings)``), so "restart with a different mode" is simulated by
re-seeding (the real lifespan step that touches identity) against the *same*
persistent test database, then changing ``KLANGK_AUTH_MODES`` and continuing
to hit the same API.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk_backend import api, auth, main, model
from klangk_backend.main import register_exception_handlers
from klangk_backend.util import API_PREFIX

DEFAULT_EMAIL = "admin@example.com"
SEEDED_PASSWORD = "seeded-pw"
NEW_PASSWORD = "rotated-by-admin"


@pytest.fixture
async def mode_server(db, monkeypatch):
    """Boot a real-router server whose default admin user is seeded the way
    the real lifespan seeds it (``main.seed_default_user``), then flip modes
    by changing ``KLANGK_AUTH_MODES`` between requests.

    Returns ``(client, default_user)`` where ``default_user`` is the DB row
    for the seeded default user (so tests know its ``id`` / ``email``).
    """
    monkeypatch.setenv("KLANGK_DEFAULT_USER", DEFAULT_EMAIL)
    monkeypatch.setenv("KLANGK_DEFAULT_PASSWORD", SEEDED_PASSWORD)
    # Seed exactly as the lifespan does at startup.
    await main.seed_default_user()
    default_user = await model.get_user_by_email(DEFAULT_EMAIL)
    assert default_user is not None, "seed_default_user must create the user"

    app = FastAPI()
    app.include_router(api.root_router)
    app.include_router(api.router, prefix=API_PREFIX)
    register_exception_handlers(app)

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client, default_user


def _none(monkeypatch):
    monkeypatch.setenv("KLANGK_AUTH_MODES", "none")


def _password(monkeypatch):
    monkeypatch.setenv("KLANGK_AUTH_MODES", "password")


# ---------------------------------------------------------------------------
# none -> password : the documented single-user -> multi-user upgrade
# ---------------------------------------------------------------------------


class TestNoneToPasswordUpgrade:
    """Mirrors the `none` -> `password` recipe in docs/features/auth-modes.md."""

    async def test_free_token_is_admin_and_can_set_password(
        self, mode_server, monkeypatch
    ):
        """Steps 1-2: get the free token, then ``PATCH /admin/users/{id}``
        with a new password — succeeds because the seeded default user is an
        admin."""
        client, user = mode_server
        _none(monkeypatch)

        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        h = {"Authorization": f"Bearer {token}"}

        # The free token really is admin.
        me_perms = (
            await client.get("/api/v1/my-permissions", headers=h)
        ).json()
        assert "*" in me_perms["permissions"].get("/admin", [])

        # admin set-password equivalent.
        resp = await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            headers=h,
            json={"password": NEW_PASSWORD},
        )
        assert resp.status_code == 200

    async def test_password_login_works_after_upgrade(
        self, mode_server, monkeypatch
    ):
        """Step 3-4: flip to password mode, then login with the password the
        admin just set works — the documented end state."""
        client, user = mode_server
        _none(monkeypatch)
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            headers={"Authorization": f"Bearer {token}"},
            json={"password": NEW_PASSWORD},
        )

        _password(monkeypatch)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": DEFAULT_EMAIL, "password": NEW_PASSWORD},
        )
        assert resp.status_code == 200
        assert resp.json()["token_type"] == "bearer"

    async def test_old_password_is_invalidated_by_set_password(
        self, mode_server, monkeypatch
    ):
        """``admin users set-password`` replaces the hash, so whatever password
        the default user had before (the seeded one) no longer works."""
        client, user = mode_server
        _none(monkeypatch)
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            headers={"Authorization": f"Bearer {token}"},
            json={"password": NEW_PASSWORD},
        )
        _password(monkeypatch)

        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": DEFAULT_EMAIL, "password": SEEDED_PASSWORD},
        )
        assert resp.status_code == 401

    async def test_local_login_disabled_after_flip_to_password(
        self, mode_server, monkeypatch
    ):
        """Once real login is on, the free-token endpoint is gone (403)."""
        client, _ = mode_server
        _password(monkeypatch)
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 403

    async def test_free_token_survives_into_password_mode(
        self, mode_server, monkeypatch
    ):
        """A token minted in none mode keeps authorising after the flip — a
        mode switch is not a global logout (docs: 'tokens in flight keep
        working until they expire')."""
        client, _ = mode_server
        _none(monkeypatch)
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        h = {"Authorization": f"Bearer {token}"}
        assert (
            await client.get("/api/v1/auth/me", headers=h)
        ).status_code == 200

        _password(monkeypatch)
        # Same token, new mode — still valid.
        resp = await client.get("/api/v1/auth/me", headers=h)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# password -> none : dropping back to solo
# ---------------------------------------------------------------------------


class TestPasswordToNone:
    async def test_local_login_returns_after_flip_to_none(
        self, mode_server, monkeypatch
    ):
        """Reversing the switch re-enables the no-auth endpoint."""
        client, _ = mode_server
        _password(monkeypatch)
        assert (await client.post("/api/v1/auth/local")).status_code == 403

        _none(monkeypatch)
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 200
        assert resp.json()["email"] == DEFAULT_EMAIL

    async def test_password_token_survives_into_none_mode(
        self, mode_server, monkeypatch
    ):
        """Token-in-flight claim, reverse direction."""
        client, _ = mode_server
        _password(monkeypatch)
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": DEFAULT_EMAIL, "password": SEEDED_PASSWORD},
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        _none(monkeypatch)
        assert (
            await client.get("/api/v1/auth/me", headers=h)
        ).status_code == 200


# ---------------------------------------------------------------------------
# What carries over across a switch
# ---------------------------------------------------------------------------


class TestDataCarriesOver:
    async def test_same_identity_survives_mode_switch(
        self, mode_server, monkeypatch
    ):
        """The operator is the same DB user before and after the switch (same
        id, same email, still admin)."""
        client, user = mode_server
        _none(monkeypatch)
        free = (await client.post("/api/v1/auth/local")).json()["access_token"]
        none_me = (
            await client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {free}"}
            )
        ).json()

        _password(monkeypatch)
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": DEFAULT_EMAIL, "password": SEEDED_PASSWORD},
        )
        assert login.status_code == 200
        pw_me = (
            await client.get(
                "/api/v1/auth/me",
                headers={
                    "Authorization": f"Bearer {login.json()['access_token']}"
                },
            )
        ).json()

        assert none_me["id"] == pw_me["id"] == user["id"]
        assert none_me["email"] == pw_me["email"] == DEFAULT_EMAIL

    async def test_workspace_survives_mode_switch(
        self, mode_server, monkeypatch
    ):
        """A workspace created in none mode is still owned by the default user
        after flipping to password mode — modes change auth, not data."""
        client, user = mode_server
        _none(monkeypatch)
        ws = await model.create_workspace(user["id"], "persists-across-switch")

        _password(monkeypatch)
        # Re-resolve the workspace straight from the DB the restart would see.
        rows = (await model.list_workspaces(user["id"]))["items"]
        names = [r["name"] for r in rows]
        assert ws["name"] in names


# ---------------------------------------------------------------------------
# Reality check of the "change-password lockout" wording in the docs
# ---------------------------------------------------------------------------


class TestChangePasswordReality:
    """The default user is NOT password-less (seed_default_user always sets a
    hash). These pin the *actual* behaviour so the docs can't drift back into
    the inaccurate 'NULL hash / change-password refuses the default user'
    framing."""

    async def test_change_password_works_on_seeded_default_user(
        self, mode_server, monkeypatch
    ):
        """Self-service ``/auth/change-password`` works on the seeded default
        user because it has a real password hash."""
        client, _ = mode_server
        _none(monkeypatch)
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        h = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=h,
            json={
                "current_password": SEEDED_PASSWORD,
                "new_password": "freshpass1",
            },
        )
        assert resp.status_code == 200

    async def test_change_password_refuses_passwordless_user(
        self, mode_server, monkeypatch
    ):
        """A genuinely password-less (OIDC-only) account IS refused by
        self-service change-password — that 403 path exists, it just does not
        apply to the seeded default user."""
        client, _ = mode_server
        _none(monkeypatch)

        # A user with no password hash (an OIDC-only style account).
        oidc_user = await model.create_user(
            "oidc-only@example.com", None, verified=True
        )
        # Mint a token for that user so the request authenticates.
        oidc_token = auth.create_token(oidc_user["id"], oidc_user["email"])

        resp = await client.post(
            "/api/v1/auth/change-password",
            headers={"Authorization": f"Bearer {oidc_token}"},
            json={
                "current_password": "anything",
                "new_password": "freshpass1",
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Re-seeding idempotency: a restart against the same DB must not break
# ---------------------------------------------------------------------------


class TestRestartIdempotency:
    async def test_reseed_after_mode_switch_is_safe(
        self, mode_server, monkeypatch
    ):
        """Re-running the lifespan seed (a restart with a new mode, same DB)
        must not duplicate the user or drop its admin membership."""
        client, user = mode_server
        _none(monkeypatch)
        await main.seed_default_user()  # simulate restart in none mode

        _password(monkeypatch)
        await main.seed_default_user()  # simulate restart in password mode

        again = await model.get_user_by_email(DEFAULT_EMAIL)
        assert again["id"] == user["id"]
        # Still admin (membership re-asserted, not lost) — ask the canonical
        # /my-permissions source the CLI and frontend both use.
        _none(monkeypatch)
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        perms = (
            await client.get(
                "/api/v1/my-permissions",
                headers={"Authorization": f"Bearer {token}"},
            )
        ).json()
        assert perms and "*" in perms["permissions"].get("/admin", [])
