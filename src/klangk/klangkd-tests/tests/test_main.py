"""Tests for main.py: lifespan, seed user, static files, logfire."""

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk import (
    auth as auth_mod,
    caddy as caddy_mod,
    container,
    consent,
    inactivity,
    sidecar_connections,
    emailsvc as emailsvc_mod,
    files as files_mod,
    ssl_trust as ssl_trust_mod,
    util as util_mod,
    main,
    model,
    oidc,
    features,
    hooks as hooks_mod,
    workspaces,
)
from klangk.container import ContainerRegistry
from klangk.exceptions import ConfigurationError, EX_CONFIG
from klangk.lifecycle import broadcast_container_status
from _helpers import make_settings
from klangk.wshandler.session import WebSocketState


def _make_app_state(settings=None):
    """Build a minimal mock app for tests."""
    if settings is None:
        # Pin a default password so tests that exercise the full lifespan
        # (where seed_default_user runs) don't fail-fast in password mode.
        # In none mode (the default) the password is ignored (null hash).
        settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
    # Two-phase: shell first so owned instances can take app at
    # construction (#1426).
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    sockets = WebSocketState(app_state)
    app_state.state.sockets = sockets
    registry = ContainerRegistry(app_state)
    app_state.state.container_registry = registry
    # #2527: the SIGHUP quiesce phase waits on this counter.
    app_state.state.inflight_requests = main.InFlightRequests()
    # #1468: container.py reaches the CLI wrappers via self.podman.
    from klangk.podman import Podman

    app_state.state.podman = Podman(app_state)
    app_state.state.oidc = oidc.OIDC(app_state)
    app_state.state.features = features.Features(app_state)
    # #2762: customize-dir lifecycle hooks (workspace-created hook).
    app_state.state.hooks = hooks_mod.Hooks(app_state)
    app_state.state.workspaces = workspaces.Workspaces(app_state)
    app_state.state.files = files_mod.Files(app_state)
    # #1520: the lifespan binds app.state.db as the active DB for its context;
    # mirror build_app so lifespan-driven tests have it.
    from klangk.model import db as db_mod

    app_state.state.db = db_mod.DB(app_state)
    # #1572: Model(app_state) composing the converted domains.
    from klangk.model import Model

    app_state.state.model = Model(app_state)
    app_state.state.email = emailsvc_mod.EmailService(app_state)
    app_state.state.util = util_mod.Util(app_state)
    # #1567: the lifespan calls app.state.ssl_trust.apply_backend_ssl_trust().
    app_state.state.ssl_trust = ssl_trust_mod.SSLTrust(app_state)
    # The lifespan constructs NetFilter on app.state (#1365).
    from klangk.netfilter import NetFilter

    app_state.state.netfilter = NetFilter(app_state)
    app_state.state.auth = auth_mod.Auth(app_state)
    app_state.state.proxy_watchdog = caddy_mod.CaddyWatchdog(app_state)
    from klangk.llm_router import LLMRouter

    app_state.state.llm_router = LLMRouter(app_state)
    from klangk.terminal import Terminal
    from klangk.acl import ACL

    app_state.state.terminal = Terminal(app_state)
    app_state.state.acl = ACL(app_state)
    # #1571: Lifecycle(app_state) owns startup/shutdown/restart + seeding.
    app_state.state.lifecycle = main.Lifecycle(app_state)
    return app_state


def _lifecycle(settings):
    """A ``Lifecycle`` whose app can reach ``model.acl``.

    The seed methods read ``self.app.state.settings`` and reach the DB via
    ``self.app.state.model.acl.*`` (ACL seeding), so the namespace needs
    ``db`` + ``model`` wired (#1574). ``wire_db_and_model`` reuses the
    per-test DB.
    """
    from _helpers import wire_db_and_model

    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    wire_db_and_model(app)
    return main.Lifecycle(app)


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_DEFAULT_USER": "seed-test",
                    "KLANGKD_DEFAULT_PASSWORD": "seed-pass",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is not None

    async def test_password_mode_without_default_password_fails_fast(
        self, db, app_state
    ):
        """Password mode with no staged password is a config refusal
        (#1645, and a ConfigurationError so the launcher exits EX_CONFIG
        — #2666)."""
        with pytest.raises(
            ConfigurationError, match="requires KLANGKD_DEFAULT_PASSWORD"
        ):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "password",
                        "KLANGKD_DEFAULT_USER": "seed-test",
                    }
                )
            ).seed_default_user()

    async def test_default_password_violating_policy_fails_fast(
        self, db, app_state
    ):
        """#2581: the seeded admin must satisfy the configured policy."""
        with pytest.raises(ConfigurationError, match="DEFAULT_PASSWORD"):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "password",
                        "KLANGKD_DEFAULT_USER": "seed-test",
                        "KLANGKD_DEFAULT_PASSWORD": "alllowercase1!",
                        "KLANGKD_PASSWORD_REQUIRE_UPPER": "1",
                    }
                )
            ).seed_default_user()
        # Nothing was seeded.
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is None

    async def test_default_password_too_short_fails_fast(self, db, app_state):
        with pytest.raises(ConfigurationError, match="MIN_PASSWORD_LENGTH"):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "password",
                        "KLANGKD_DEFAULT_USER": "seed-test",
                        "KLANGKD_DEFAULT_PASSWORD": "ab",
                        "KLANGKD_MIN_PASSWORD_LENGTH": "8",
                    }
                )
            ).seed_default_user()

    async def test_compliant_default_password_seeds(self, db, app_state):
        """A policy-compliant password passes the new gate untouched."""
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "seed-test",
                    "KLANGKD_DEFAULT_PASSWORD": "Seed-Pass1!",
                    "KLANGKD_PASSWORD_REQUIRE_UPPER": "1",
                    "KLANGKD_PASSWORD_REQUIRE_SPECIAL": "1",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, app_state):
        s = make_settings(
            {
                "KLANGKD_DEFAULT_USER": "seed-test",
                "KLANGKD_DEFAULT_PASSWORD": "seed-pass",
            }
        )
        await _lifecycle(s).seed_default_user()
        # Call again — should not raise
        await _lifecycle(s).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("seed-test")
        assert user is not None

    async def test_seeds_null_password_in_none_mode(self, db, app_state):
        # Default mode (none): seed with null password_hash. The row exists
        # for /auth/local token minting but no endpoint checks the hash (#1645).
        await _lifecycle(
            make_settings({"KLANGKD_DEFAULT_USER": "none-test"})
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("none-test")
        assert user is not None
        assert user["password_hash"] is None
        # User is in the admin group
        admin_group = await app_state.state.model.users.get_group_by_name(
            "admins"
        )
        assert admin_group is not None
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert admin_group["id"] in group_ids


class TestSeedDefaultAcls:
    """Seeded-ACL shape (#2944): the first-class trees each seed an
    Allow manage-* (admins) + Deny everyone pair; /admin is retired
    (#2974 — the admin marker lives in /my-permissions' is_admin flag)."""

    async def test_first_class_resources_seeded(self, db, app_state):
        """#2944: each first-class tree seeds Allow manage-* (admins) +
        Deny everyone; /admin is retired (#2974 — the marker is the
        is_admin flag on /my-permissions)."""
        lifecycle = _lifecycle(make_settings({}))
        admin_group_id = await lifecycle.ensure_admin_group()
        await lifecycle.seed_default_acls(admin_group_id)

        for resource, permission in (
            # NB: /users is checked separately below — it carries the
            # #2946 search-users row (3 entries, not 2).
            ("/groups", "manage-groups"),
            ("/invitations", "manage-invitations"),
            ("/server", "manage-server-schedule"),
            ("/events", "manage-events"),
            ("/acl", "manage-acls"),
        ):
            entries = await app_state.state.model.acl.get_acl_entries(resource)
            assert len(entries) == 2, resource
            allow, deny = entries
            assert allow["permission"] == permission
            assert allow["group_id"] == admin_group_id
            assert deny["permission"] == "*"
            assert deny["system_principal"] == model.SYSTEM_EVERYONE

        # #2974: /admin is gone from the seed — the instance-admin
        # marker is the is_admin flag on /my-permissions (admins-group
        # membership), not a wildcard ACE.
        admin_entries = await app_state.state.model.acl.get_acl_entries(
            "/admin"
        )
        assert admin_entries == []

        # #2946: /users carries the search-users row for Authenticated
        # between the admins' Allow and the blanket Deny.
        users = await app_state.state.model.acl.get_acl_entries("/users")
        assert [(e["position"], e["permission"]) for e in users] == [
            (0, "manage-users"),
            (1, "search-users"),
            (2, "*"),
        ]
        assert users[1]["system_principal"] == model.SYSTEM_AUTHENTICATED
        assert users[2]["action"] == model.ACTION_DENY
        # #2974: /volumes seeds the admin surface — Allow view-volumes +
        # Allow manage-volumes for admins, no Deny row (no-match is
        # default-deny; unauthenticated dies at the JWT middleware).
        volumes = await app_state.state.model.acl.get_acl_entries("/volumes")
        assert [
            (e["position"], e["permission"], e["group_id"]) for e in volumes
        ] == [
            (0, "view-volumes", admin_group_id),
            (1, "manage-volumes", admin_group_id),
        ]
        # ...and /images seeds the #2946 self-service row — Allow
        # Authenticated only (#2974 dropped the dead Deny Everyone row:
        # it can never fire, and no-match is default-deny).
        images = await app_state.state.model.acl.get_acl_entries("/images")
        assert [(e["position"], e["permission"]) for e in images] == [
            (0, "view-images")
        ]
        assert images[0]["system_principal"] == model.SYSTEM_AUTHENTICATED
        assert images[0]["action"] == model.ACTION_ALLOW


class TestSeedDefaultUserAuthModeGating:
    """#1645 Table A: password handling depends on auth_modes.

    none / oidc → null password_hash (row is for /auth/local token minting).
    password / both → require KLANGKD_DEFAULT_PASSWORD or fail-fast.
    """

    async def test_none_mode_seeds_null_password(self, db, app_state):
        await _lifecycle(
            make_settings({"KLANGKD_DEFAULT_USER": "u-none"})
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("u-none")
        assert user["password_hash"] is None

    async def test_oidc_mode_seeds_null_password(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "oidc",
                    "KLANGKD_DEFAULT_USER": "u-oidc",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("u-oidc")
        assert user["password_hash"] is None

    async def test_password_mode_uses_default_password(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "u-pw",
                    "KLANGKD_DEFAULT_PASSWORD": "real-pass",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("u-pw")
        assert user["password_hash"] is not None
        from klangk import auth

        assert auth.verify_password("real-pass", user["password_hash"])

    async def test_password_mode_fails_fast_without_default_password(
        self, db, app_state
    ):
        with pytest.raises(RuntimeError, match="KLANGKD_DEFAULT_PASSWORD"):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "password",
                        "KLANGKD_DEFAULT_USER": "u-nopw",
                    }
                )
            ).seed_default_user()

    async def test_both_mode_uses_default_password(self, db, app_state):
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "both",
                    "KLANGKD_DEFAULT_USER": "u-both",
                    "KLANGKD_DEFAULT_PASSWORD": "both-pass",
                }
            )
        ).seed_default_user()
        user = await app_state.state.model.users.get_user_by_email("u-both")
        assert user["password_hash"] is not None

    async def test_both_mode_fails_fast_without_default_password(
        self, db, app_state
    ):
        with pytest.raises(RuntimeError, match="KLANGKD_DEFAULT_PASSWORD"):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "both",
                        "KLANGKD_DEFAULT_USER": "u-both-nopw",
                    }
                )
            ).seed_default_user()

    async def test_table_b_guard_blocks_password_boot_when_admin_has_null_hash(
        self, db, app_state
    ):
        """Table B lockout guard (#1645 review): booting in password mode with
        a null-hash admin (seeded in none/oidc) is refused before the server
        serves traffic. Without this guard the operator would discover the
        lockout at the login screen with no recovery path (/auth/local is
        disabled outside none mode)."""
        # Seed in none mode → admin row with password_hash=None.
        await _lifecycle(
            make_settings({"KLANGKD_DEFAULT_USER": "u-nullhash@example.com"})
        ).seed_default_user()
        admin = await app_state.state.model.users.get_user_by_email(
            "u-nullhash@example.com"
        )
        assert admin["password_hash"] is None

        # Subsequent boot in password mode (admin group non-empty → seeding
        # skipped, but the guard fires on the existing members).
        with pytest.raises(RuntimeError, match="admin with a password"):
            await _lifecycle(
                make_settings(
                    {
                        "KLANGKD_AUTH_MODES": "password",
                        "KLANGKD_DEFAULT_USER": "u-nullhash@example.com",
                    }
                )
            ).seed_default_user()

    async def test_table_b_guard_passes_when_admin_has_hash(
        self, db, app_state
    ):
        """Mirror of the lockout guard: a password-mode boot where the admin
        DOES have a hash proceeds normally (seed skipped, no error)."""
        # Seed in password mode → admin row with real hash.
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "u-hashed@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "real-pass",
                }
            )
        ).seed_default_user()

        # Subsequent boot in password mode (same admin, same hash) — no raise.
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "u-hashed@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "real-pass",
                }
            )
        ).seed_default_user()

    async def test_table_b_guard_noop_in_none_mode(self, db, app_state):
        """The guard doesn't fire in none mode even with a null-hash admin —
        none mode never validates passwords."""
        await _lifecycle(
            make_settings({"KLANGKD_DEFAULT_USER": "u-none@example.com"})
        ).seed_default_user()
        # Second boot, still none mode — no raise.
        await _lifecycle(
            make_settings({"KLANGKD_DEFAULT_USER": "u-none@example.com"})
        ).seed_default_user()


class TestSeedDefaultUserGating:
    """#1622: seed exactly once, gated on admin-group emptiness.

    Once the ``admin`` group has ≥1 member, ``seed_default_user`` must not
    create a new admin or modify any existing user, no matter what
    ``KLANGKD_DEFAULT_*`` says — closes the config-mints-admin hole and
    prevents lockout.
    """

    async def test_does_not_create_when_admin_group_has_member(
        self, db, app_state
    ):
        """Legacy/post-first-boot install: an admin already exists → seed is
        a no-op on users, regardless of KLANGKD_DEFAULT_USER."""
        # Seed once to populate the admin group.
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_DEFAULT_USER": "first@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "first-pass",
                }
            )
        ).seed_default_user()
        original = await app_state.state.model.users.get_user_by_email(
            "first@example.com"
        )
        assert original is not None

        # Now change KLANGKD_DEFAULT_USER and re-seed → must NOT create a
        # second admin from the new config.
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_DEFAULT_USER": "second@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "second-pass",
                }
            )
        ).seed_default_user()
        assert (
            await app_state.state.model.users.get_user_by_email(
                "second@example.com"
            )
            is None
        )
        # Original admin untouched.
        assert (
            await app_state.state.model.users.get_user_by_email(
                "first@example.com"
            )
        )["id"] == original["id"]

    async def test_does_not_modify_existing_admin_when_config_changes(
        self, db, app_state
    ):
        """Changing KLANGKD_DEFAULT_* after first boot cannot clobber the
        existing admin's email/password (no lockout)."""
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "keep@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "original-pass",
                }
            )
        ).seed_default_user()
        admin = await app_state.state.model.users.get_user_by_email(
            "keep@example.com"
        )
        assert admin is not None
        original_hash = admin["password_hash"]

        # Re-seed with a different email/password in config.
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "changed@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "changed-pass",
                }
            )
        ).seed_default_user()

        admin_after = await app_state.state.model.users.get_user_by_email(
            "keep@example.com"
        )
        assert admin_after is not None
        # Email, id, and password hash all unchanged.
        assert admin_after["id"] == admin["id"]
        assert admin_after["email"] == "keep@example.com"
        assert admin_after["password_hash"] == original_hash

    async def test_reseeds_after_admin_group_emptied(self, db, app_state):
        """Delete-resurrection at the group level: deleting the admin user
        account (which cascades their group membership) + restart re-seeds
        from KLANGKD_DEFAULT_* (the gate is group membership, not a tombstone).
        The operator "reset to seeded config" flow is deleting the admin
        account, not demoting it."""
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "resurrect@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "resurrect-pass",
                }
            )
        ).seed_default_user()
        admin = await app_state.state.model.users.get_user_by_email(
            "resurrect@example.com"
        )
        admin_group = await app_state.state.model.users.get_group_by_name(
            "admins"
        )
        assert admin is not None and admin_group is not None

        # Delete the admin user account → CASCADE empties the admin group
        # → next seed re-creates from config (the email is gone too, so no
        # UNIQUE collision).
        await app_state.state.model.users.delete_user(admin["id"])
        assert (
            await app_state.state.model.users.get_group_members(
                admin_group["id"]
            )
            == []
        )
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "resurrect@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "resurrect-pass",
                }
            )
        ).seed_default_user()

        # A member is back in the admin group.
        members = await app_state.state.model.users.get_group_members(
            admin_group["id"]
        )
        assert len(members) == 1

    async def test_seed_creates_admin_on_truly_fresh_db(self, db, app_state):
        """Fresh install (empty admin group) → seed creates, mirroring
        pre-#1622 first-boot behavior."""
        admin_group = await app_state.state.model.users.get_group_by_name(
            "admins"
        )
        # admin group may not exist yet; ensure it then assert empty.
        if admin_group is None:
            admin_group = await app_state.state.model.users.create_group(
                "admins", description="Administrators"
            )
        assert (
            await app_state.state.model.users.get_group_members(
                admin_group["id"]
            )
            == []
        )
        await _lifecycle(
            make_settings(
                {
                    "KLANGKD_AUTH_MODES": "password",
                    "KLANGKD_DEFAULT_USER": "fresh@example.com",
                    "KLANGKD_DEFAULT_PASSWORD": "fresh-pass",
                }
            )
        ).seed_default_user()
        assert (
            await app_state.state.model.users.get_user_by_email(
                "fresh@example.com"
            )
            is not None
        )
        assert (
            len(
                await app_state.state.model.users.get_group_members(
                    admin_group["id"]
                )
            )
            == 1
        )


# --- no-auth bind safety gate (#1374) ---


def _bind_safety_app_state(
    auth_mode=None, listen=None, allow_insecure=None, port="8997"
):
    """Build a minimal app_state whose oidc reads the given auth mode (#1450).

    Pass the mode/listen/allow-insecure explicitly — the bind-safety tests
    exercise different combinations, and these are now read from
    ``settings`` frozen at construction (#1518) instead of re-reading the
    env per call. ``port`` defaults to ``"8997"`` (full/browser mode) so the
    browser-bind gate applies; pass ``port=None`` to exercise headless
    (where the gate is a no-op — no browser listener, #1542).
    """
    env = {"KLANGKD_AUTH_MODES": auth_mode} if auth_mode else {}
    if port is not None:
        env["KLANGKD_PORT"] = port
    if listen is not None:
        env["KLANGKD_LISTEN"] = listen
    if allow_insecure is not None:
        env["KLANGKD_ALLOW_INSECURE_NO_AUTH"] = allow_insecure
    settings = make_settings(env)
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app_state.state.oidc = oidc.OIDC(app_state)
    app_state.state.features = features.Features(app_state)
    app_state.state.workspaces = workspaces.Workspaces(app_state)
    return app_state


class TestNoAuthBindSafety:
    """enforce_no_auth_bind_safety() — refuse none mode on a non-loopback
    bind unless explicitly overridden."""

    def test_noop_when_not_none_mode(self):
        # Returns None, raises nothing.
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="password", listen="0.0.0.0")
            )
            is None
        )

    def test_allows_loopback_ipv4(self):
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="127.0.0.1")
            )
            is None
        )

    def test_allows_loopback_ipv6_and_localhost(self):
        for host in ("::1", "localhost"):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(auth_mode="none", listen=host)
                )
                is None
            )

    def test_allows_full_loopback_range(self):
        """The whole 127.0.0.0/8 range is loopback (RFC 990), not just
        127.0.0.1 — ``127.0.0.2`` is a valid loopback bind and must be
        admitted (the original exact-match allowlist wrongly refused it)."""
        for host in ("127.0.0.2", "127.255.255.254"):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(auth_mode="none", listen=host)
                )
                is None
            )

    def test_allows_loopback_default_when_listen_unset(self):
        # KLANGKD_LISTEN defaults to 127.0.0.1 (#1375).
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none")
            )
            is None
        )

    def test_refuses_ipv6_wildcard(self):
        """``::`` binds every interface (incl. IPv6) and is NOT loopback —
        must be refused even though it isn't ``0.0.0.0``."""
        with pytest.raises(ConfigurationError) as exc_info:
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="::")
            )
        assert "::" in str(exc_info.value)

    def test_refuses_non_loopback_hostname(self):
        """A bare hostname (other than ``localhost``) is not an IP literal and
        not a recognized loopback name — fail-closed (refuse)."""
        with pytest.raises(ConfigurationError):
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="myhost")
            )

    def test_refuses_non_loopback_bind(self):
        with pytest.raises(ConfigurationError) as exc_info:
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(auth_mode="none", listen="0.0.0.0")
            )
        msg = str(exc_info.value)
        assert "KLANGKD_AUTH_MODES=none" in msg
        assert "loopback" in msg
        assert "KLANGKD_ALLOW_INSECURE_NO_AUTH=1" in msg
        assert "0.0.0.0" in msg

    def test_headless_exempt_from_bind_check(self):
        """Headless (KLANGKD_PORT unset) has no browser listener, so the bind
        gate is a no-op — none mode is safe regardless of the listen address,
        because /auth/local is never exposed over TCP (#1542)."""
        assert (
            main.enforce_no_auth_bind_safety(
                _bind_safety_app_state(
                    auth_mode="none", listen="0.0.0.0", port=None
                )
            )
            is None
        )

    def test_override_flag_allows_non_loopback(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            assert (
                main.enforce_no_auth_bind_safety(
                    _bind_safety_app_state(
                        auth_mode="none",
                        listen="0.0.0.0",
                        allow_insecure="1",
                    )
                )
                is None
            )
        assert "non-loopback bind" in caplog.text


# --- Seed agent user ---


class TestSeedAgentUser:
    async def test_creates_agent_user(self, db, app_state):
        await _lifecycle(make_settings({})).seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user is not None
        assert user["email"] == "klangk@example.com"
        assert user["handle"] == "klangk"

    async def test_identity_is_fixed_ignores_feature_config(
        self, db, app_state
    ):
        """The agent identity is constant (#2718): the former chat feature
        config keys (KLANGKWS_FEATURE_CHAT_AGENT_EMAIL/HANDLE) are gone;
        stale entries in a resolver's output are ignored."""
        lc = _lifecycle(make_settings({}))
        lc.app.state.features.frontend_config.return_value = {
            "chat_agent_email": "bot@test.com",
            "chat_agent_handle": "TestBot",
        }
        await lc.seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user is not None
        assert user["email"] == "klangk@example.com"
        assert user["handle"] == "klangk"

    async def test_upserts_existing(self, db, app_state):
        # Seed, then re-seed — a pre-#2718 'clanker' row is reconciled
        # to the fixed identity.
        await _lifecycle(make_settings({})).seed_agent_user()
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "UPDATE users SET handle = ?, email = ? WHERE id = ?",
                ("clanker", "clanker@example.com", model.AGENT_USER_ID),
            )
        await _lifecycle(make_settings({})).seed_agent_user()
        user = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert user["email"] == "klangk@example.com"
        assert user["handle"] == "klangk"

    async def test_clears_cache(self, db, app_state):
        # Prime cache with fallback
        await app_state.state.model.users.get_agent_user()
        await _lifecycle(make_settings({})).seed_agent_user()
        # Cache should now reflect DB values
        agent = await app_state.state.model.users.get_agent_user()
        assert agent["email"] == "klangk@example.com"

    async def test_users_handle_has_unique_constraint(self, db, app_state):
        """The users.handle UNIQUE constraint is the structural backstop.

        Confirms a duplicate handle raises IntegrityError at the DB layer,
        independent of seed_agent_user's pre-check.  See #1137.
        """
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "INSERT INTO users (id, email, handle)"
                " VALUES ('uid-a', 'a@x.com', 'alice')"
            )
            with pytest.raises(SAIntegrityError) as exc_info:
                await db_conn.execute(
                    "INSERT INTO users (id, email, handle)"
                    " VALUES ('uid-b', 'b@x.com', 'alice')"
                )
        # The underlying driver-level cause is the sqlite UNIQUE violation.
        assert isinstance(exc_info.value.orig, sqlite3.IntegrityError)

    async def test_seed_refuses_handle_collision_with_human(
        self, db, app_state
    ):
        """Seeding the agent with a live user's handle fails cleanly.

        The destructive path is ensure_home_symlink migrating that user's
        files into the agent's tree; the guard must abort before any such
        work.  See #1137.
        """
        human = await app_state.state.model.users.create_user(
            "alice@example.com", "hash", verified=True
        )
        assert human["handle"] == "alice"
        # Simulate the pre-migration state: a human already holding the
        # fixed 'klangk' handle (possible because it was never reserved
        # before #2718).
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "UPDATE users SET handle = 'klangk' WHERE id = ?",
                (human["id"],),
            )
        with pytest.raises(RuntimeError, match="klangk"):
            await _lifecycle(make_settings({})).seed_agent_user()
        # Human user is untouched.
        refreshed = await app_state.state.model.users.get_user_by_id(
            human["id"]
        )
        assert refreshed["handle"] == "klangk"
        # Agent was not created with the colliding handle.
        assert (
            await app_state.state.model.users.get_user_by_id(
                model.AGENT_USER_ID
            )
            is None
        )

    async def test_seed_renames_clanker_era_row_to_klangk(self, db, app_state):
        """Re-seeding reconciles a pre-#2718 'klangk' row to the fixed
        identity (the boot-time counterpart of the m0008 migration)."""
        await _lifecycle(make_settings({})).seed_agent_user()
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "UPDATE users SET handle = ?, email = ? WHERE id = ?",
                ("clanker", "clanker@example.com", model.AGENT_USER_ID),
            )
        app_state.state.model.users.clear_agent_cache()
        await _lifecycle(make_settings({})).seed_agent_user()
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        assert agent["handle"] == "klangk"
        assert agent["email"] == "klangk@example.com"

    async def test_collision_leaves_human_files_untouched(
        self, db, tmp_path, app_state
    ):
        """A handle collision never reaches ensure_home_symlink's adoption.

        Builds the on-disk layout that the destructive branch would migrate
        (a /home/<handle> symlink -> .users/<human-uid> with files) and
        confirms a colliding agent seed aborts before any file moves.  See
        #1137.
        """
        human = await app_state.state.model.users.create_user(
            "alice@example.com", "hash", verified=True
        )
        # The human claims the fixed 'klangk' handle (possible pre-#2718;
        # the m0008 migration would have bumped them, this tests the
        # un-migrated edge).
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "UPDATE users SET handle = 'klangk' WHERE id = ?",
                (human["id"],),
            )
        # Stand up the destructive-branch precondition directly on disk.
        home = tmp_path / "home"
        users_dir = home / ".users"
        users_dir.mkdir(parents=True)
        human_dir = users_dir / human["id"]
        human_dir.mkdir()
        (human_dir / "secret.txt").write_text("alice's secrets")
        (home / "klangk").symlink_to(f".users/{human['id']}")

        with pytest.raises(RuntimeError):
            await _lifecycle(make_settings({})).seed_agent_user()

        # Human's files are exactly where they were — nothing migrated.
        assert (human_dir / "secret.txt").read_text() == "alice's secrets"
        assert os.readlink(home / "klangk") == f".users/{human['id']}"
        # No agent user directory was created.
        assert not (users_dir / model.AGENT_USER_ID).exists()


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db, app_state):
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.proxy_watchdog = caddy_mod.CaddyWatchdog(app)
        app.state.consent_sweeper = consent.EgressConsentSweeper(app)
        app.state.inactivity_sweeper = inactivity.InactivitySweeper(app)
        app.state.memory_evictor = container.eviction.MemoryPressureEvictor(
            app
        )
        app.state.consent_deciders = consent.ConsentDeciderRegistry(app)
        app.state.consent_coordinator = consent.ConsentCoordinator(app)
        app.state.sidecar_connections = sidecar_connections.SidecarConnections(
            app
        )
        app.state.oidc = oidc.OIDC(app)
        app.state.features = features.Features(app)
        app.state.hooks = app_state.state.hooks
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        # #2661: stub the server scheduler so the lifespan wires + starts /
        # stops it (guarded — minimal apps omit it; that branch is the
        # getattr default above).
        scheduler_stub = types.SimpleNamespace(
            start=MagicMock(), stop=AsyncMock()
        )
        app.state.server_scheduler = scheduler_stub
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop") as mock_start,
            patch.object(
                registry,
                "shutdown",
                new_callable=AsyncMock,
            ) as mock_shutdown,
            patch.object(
                util_mod.Util, "check_pid_file", return_value=None
            ) as mock_check,
            patch.object(util_mod.Util, "write_pid_file") as mock_write,
            patch.object(util_mod.Util, "remove_pid_file") as mock_remove,
        ):
            async with main.lifespan(app):
                mock_check.assert_called_once()
                mock_write.assert_called_once()
                mock_adopt.assert_awaited_once()
                mock_start.assert_called_once()
                scheduler_stub.start.assert_called_once()
            scheduler_stub.stop.assert_awaited_once()
            mock_shutdown.assert_awaited_once()
            mock_remove.assert_called_once()
        # A clean startup leaves no config-error flag for the launcher (#2666).
        assert getattr(app.state, "startup_config_error", None) is None

    async def test_lifespan_workspace_killed_resets_state(self, db, app_state):
        """The workspace-killed callback threads app.state into
        reset_workspace_state (sockets, workspace_id) — #1475."""
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.proxy_watchdog = caddy_mod.CaddyWatchdog(app)
        app.state.consent_sweeper = consent.EgressConsentSweeper(app)
        app.state.inactivity_sweeper = inactivity.InactivitySweeper(app)
        app.state.memory_evictor = container.eviction.MemoryPressureEvictor(
            app
        )
        app.state.consent_deciders = consent.ConsentDeciderRegistry(app)
        app.state.consent_coordinator = consent.ConsentCoordinator(app)
        app.state.sidecar_connections = sidecar_connections.SidecarConnections(
            app
        )
        app.state.oidc = oidc.OIDC(app)
        app.state.features = features.Features(app)
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "shutdown", new_callable=AsyncMock),
            patch.object(util_mod.Util, "check_pid_file", return_value=None),
            patch.object(util_mod.Util, "write_pid_file"),
            patch.object(util_mod.Util, "remove_pid_file"),
            patch(
                "klangk.lifecycle.wshandler.reset_workspace_state",
                new_callable=AsyncMock,
            ) as mock_reset,
        ):
            async with main.lifespan(app):
                # The closure registered by the lifespan threads app.state
                # into reset_workspace_state: (sockets, workspace_id).
                assert registry.on_workspace_killed is not None
                await registry.on_workspace_killed("ws-killed")
        mock_reset.assert_awaited_once_with(
            app.state.sockets, "ws-killed", expected_container_id=None
        )

    async def test_lifespan_refuses_if_pid_alive(self, db, app_state):

        app = FastAPI()
        app_state = _make_app_state()
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.util = util_mod.Util(app)
        # The lifespan reaches the DB through ``app.state.db`` +
        # ``app.state.model`` (no ContextVar bind post-#1578); point both at
        # the test-built app_state so init_db runs before the pid refuse.
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        with (
            patch.object(util_mod.Util, "check_pid_file", return_value=12345),
            pytest.raises(SystemExit),
        ):
            async with main.lifespan(app):
                pass  # pragma: no cover

    async def test_lifespan_flags_config_error_for_launcher(
        self, db, app_state
    ):
        """A ConfigurationError escaping startup is flagged on app.state so
        the launcher can exit EX_CONFIG (78) instead of uvicorn's generic
        startup-failure status — a supervisor restart-looping a bad config
        cannot converge (#2666)."""
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.oidc = oidc.OIDC(app)
        app.state.util = util_mod.Util(app)
        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        refusal = ConfigurationError(
            "KLANGKD_DEFAULT_PASSWORD violates the configured password policy"
        )
        with (
            patch.object(
                registry, "reap_instance_containers", new_callable=AsyncMock
            ),
            patch.object(
                registry, "reap_dead_owner_containers", new_callable=AsyncMock
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "shutdown", new_callable=AsyncMock),
            patch.object(util_mod.Util, "check_pid_file", return_value=None),
            patch.object(util_mod.Util, "write_pid_file"),
            patch.object(util_mod.Util, "remove_pid_file"),
            patch.object(
                app.state.lifecycle,
                "seed_default_user",
                new_callable=AsyncMock,
                side_effect=refusal,
            ),
            pytest.raises(ConfigurationError, match="DEFAULT_PASSWORD"),
        ):
            async with main.lifespan(app):
                pass  # pragma: no cover
        assert app.state.startup_config_error == str(refusal)


class TestBroadcastContainerStatus:
    """The registry status callback schedules the scoped broadcast (#1714).

    The registry invokes its status-change callback synchronously (from
    ``track_activity`` and the stop paths); the broadcast itself is
    async because it ACL-checks every recipient, so the callback wraps
    it in a fire-and-forget task.
    """

    def _app(self):
        return types.SimpleNamespace(
            state=types.SimpleNamespace(
                sockets=types.SimpleNamespace(
                    notify_container_status=AsyncMock()
                )
            )
        )

    async def test_schedules_scoped_broadcast(self):
        app = self._app()
        broadcast_container_status(app, "ws-1", True, 1000.0)
        # Let the scheduled task run to completion.
        for _ in range(3):
            await asyncio.sleep(0)
        app.state.sockets.notify_container_status.assert_awaited_once_with(
            "ws-1", True, 1000.0
        )

    async def test_broadcast_error_is_logged_not_raised(self, caplog):
        app = self._app()
        app.state.sockets.notify_container_status.side_effect = RuntimeError(
            "acl unavailable"
        )
        with caplog.at_level("ERROR", logger="klangk.lifecycle"):
            broadcast_container_status(app, "ws-2", False)
            for _ in range(3):
                await asyncio.sleep(0)
        assert any(
            "container_status broadcast failed" in r.message
            for r in caplog.records
        )

    def test_no_running_loop_is_a_noop(self):
        # Sync registry call paths (unit harnesses driving track_activity
        # directly) have no loop to schedule on — nothing to broadcast to.
        app = self._app()
        broadcast_container_status(app, "ws-3", True)
        app.state.sockets.notify_container_status.assert_not_called()


# --- SIGHUP runtime restart (#1212) ---


class TestInFlightRequests:
    """Counter + ASGI middleware backing the SIGHUP quiesce phase
    (#2527)."""

    def test_counts_http_and_decrements(self):
        counter = main.InFlightRequests()
        assert counter.count == 0
        assert asyncio.run(counter.wait_for_idle(0.01)) is True

        ran = asyncio.Event()

        async def slow(scope, receive, send):
            assert counter.count == 1
            ran.set()

        mw2 = main.InFlightMiddleware(slow, counter)
        asyncio.run(mw2({"type": "http"}, None, None))
        assert ran.is_set()
        assert counter.count == 0

    def test_non_http_scopes_pass_uncounted(self):
        counter = main.InFlightRequests()
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        mw = main.InFlightMiddleware(inner, counter)
        asyncio.run(mw({"type": "websocket"}, None, None))
        asyncio.run(mw({"type": "lifespan"}, None, None))
        assert seen == ["websocket", "lifespan"]
        assert counter.count == 0

    def test_exception_still_decrements(self):
        counter = main.InFlightRequests()

        async def boom(scope, receive, send):
            raise RuntimeError("boom")

        mw = main.InFlightMiddleware(boom, counter)
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(mw({"type": "http"}, None, None))
        assert counter.count == 0

    def test_wait_for_idle_waits_then_returns(self):
        counter = main.InFlightRequests()
        counter.increment()

        async def scenario():
            task = asyncio.ensure_future(counter.wait_for_idle(5))
            await asyncio.sleep(0.01)
            assert not task.done()
            counter.decrement()
            return await task

        assert asyncio.run(scenario()) is True

    def test_wait_for_idle_times_out(self):
        counter = main.InFlightRequests()
        counter.increment()
        assert asyncio.run(counter.wait_for_idle(0.01)) is False

    def test_decrement_floors_at_zero(self):
        counter = main.InFlightRequests()
        counter.decrement()
        assert counter.count == 0

    async def test_middleware_counts_through_real_app(self, app_state):
        """The middleware installed by build_app actually wraps the HTTP
        stack: a request through a real (small) FastAPI app + ASGITransport
        is counted in flight and released on completion — exercising the
        wrap order, not just direct __call__ (#2527 review)."""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient as HC

        counter = main.InFlightRequests()
        app = FastAPI()
        app.add_middleware(main.InFlightMiddleware, counter=counter)

        @app.get("/slow")
        async def slow():  # pragma: no cover - trivial
            assert counter.count == 1
            return {"ok": True}

        transport = ASGITransport(app=app)
        async with HC(transport=transport, base_url="http://t") as client:
            assert counter.count == 0
            resp = await client.get("/slow")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        assert counter.count == 0

    async def test_middleware_counts_503_responses(self, app_state):
        """Error responses (here: 404 from an unknown route) still
        decrement — a leak would pin the SIGHUP quiesce at its timeout
        forever."""
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient as HC

        counter = main.InFlightRequests()
        app = FastAPI()
        app.add_middleware(main.InFlightMiddleware, counter=counter)

        transport = ASGITransport(app=app)
        async with HC(transport=transport, base_url="http://t") as client:
            resp = await client.get("/nope")
            assert resp.status_code == 404
        assert counter.count == 0


class TestGracefulShutdown:
    """TERM/INT graceful shutdown hook (#2527)."""

    async def test_sequence_notify_refuse_quiesce_drain_handoff(
        self, app_state, caplog
    ):
        """The hook broadcasts host_shutdown, refuses starts, quiesces
        in-flight requests (bounded by the live settings' timeout,
        #2664), drains with reason 'host shutdown', and logs each
        phase."""
        import signal as signal_mod

        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        registry = app_state.state.container_registry
        order = []
        with (
            patch.object(
                app_state.state.sockets,
                "notify_host_shutdown",
                side_effect=lambda: order.append("notify"),
            ),
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                side_effect=lambda timeout: order.append("quiesce") or True,
            ) as mock_wait,
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                side_effect=lambda **kw: (
                    order.append("drain:" + kw.get("reason", "")) or 2
                ),
            ),
            caplog.at_level("INFO"),
        ):
            await lc.graceful_shutdown(signal_num=signal_mod.SIGTERM)
        assert order == ["notify", "quiesce", "drain:host shutdown"]
        # The shutdown reads the timeout from the live settings (the
        # app's own settings object — no reload happens on this path).
        mock_wait.assert_awaited_once_with(
            app_state.state.settings.quiesce_timeout
        )
        # The drain flag stays set: nothing comes back after a shutdown.
        assert registry.draining is True
        assert any(
            "graceful shutdown beginning" in r.message for r in caplog.records
        )

    async def test_shutdown_quiesce_timeout_proceeds(self, app_state, caplog):
        """Straggler requests at quiesce expiry are logged (WARNING) and
        the shutdown proceeds to the drain anyway — the exit is never
        blocked by a stuck request."""
        import signal as signal_mod

        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        app_state.state.inflight_requests.increment()
        with (
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                app_state.state.container_registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_drain,
            patch.object(app_state.state.sockets, "notify_host_shutdown"),
            caplog.at_level("WARNING"),
        ):
            await lc.graceful_shutdown(signal_num=signal_mod.SIGINT)
        assert any(
            "still in flight" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )
        mock_drain.assert_awaited_once_with(reason="host shutdown")

    async def test_shutdown_quiesce_failure_still_drains(
        self, app_state, caplog
    ):
        """A quiesce-phase exception is labeled truthfully (not as a
        drain failure) and never skips the drain — the exit path must
        always stop the containers (#2664 review)."""
        import signal as signal_mod

        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with (
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                side_effect=RuntimeError("counter exploded"),
            ),
            patch.object(
                app_state.state.container_registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_drain,
            patch.object(app_state.state.sockets, "notify_host_shutdown"),
            caplog.at_level("WARNING"),
        ):
            await lc.graceful_shutdown(signal_num=signal_mod.SIGTERM)
        assert any("quiesce failed" in r.message for r in caplog.records)
        assert not any("drain failed" in r.message for r in caplog.records)
        mock_drain.assert_awaited_once_with(reason="host shutdown")

    async def test_drain_failure_does_not_block_exit(self, app_state, caplog):
        """A drain exception is logged and the hook completes — the
        process must still exit (never a wedged live server)."""
        import signal as signal_mod

        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("podman exploded"),
            ),
            patch.object(
                app_state.state.sockets, "notify_host_shutdown"
            ) as mock_notify,
            caplog.at_level("WARNING"),
        ):
            await lc.graceful_shutdown(signal_num=signal_mod.SIGINT)
        mock_notify.assert_called_once()
        assert any("drain failed" in r.message for r in caplog.records)

    async def test_sighup_ignored_during_shutdown(self, app_state):
        """A HUP racing the shutdown is dropped, not scheduled —
        recycling a runtime that is being torn down would race the
        process exit."""
        lc = _make_app_state().state.lifecycle
        lc.shutting_down = True
        with patch.object(
            lc, "recycle_runtime", new_callable=AsyncMock
        ) as mock_restart:
            lc.on_sighup()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        mock_restart.assert_not_awaited()

    async def test_sighup_schedules_when_not_shutting_down(self, app_state):
        """Sanity: with no shutdown in flight, HUP schedules as before."""
        lc = _make_app_state().state.lifecycle
        with patch.object(
            lc, "recycle_runtime", new_callable=AsyncMock
        ) as mock_restart:
            lc.on_sighup()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        mock_restart.assert_awaited_once()


class TestGracefulExitServer:
    """The uvicorn Server subclass that runs the shutdown hook before
    uvicorn's own exit (main.py, #2527)."""

    def _server_cls(self, app_state):
        from klangk.main import make_graceful_exit_server

        return make_graceful_exit_server(app_state)

    async def test_hook_runs_once_then_original_called(self, app_state):
        """First TERM schedules graceful_shutdown (NOT handle_exit — the
        exit must wait for the drain); a second signal during the hook
        force-exits (force_exit set) and goes straight to uvicorn's
        handler."""
        import types as types_mod

        app_state = _make_app_state()
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(),
            _captured_signals=[],
            force_exit=False,
        )
        lc = app_state.state.lifecycle
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_hook(*, signal_num):
            started.set()
            await release.wait()

        with patch.object(lc, "graceful_shutdown", side_effect=slow_hook):
            hooked = None

            def grab(sig, handler):
                nonlocal hooked
                hooked = handler
                return MagicMock()

            with patch("signal.signal", side_effect=grab) as mock_set:
                gen = cls.capture_signals(server)
                with gen:
                    assert hooked is not None
                    hooked(15, None)  # SIGTERM
                    # Hook started; exit waits for it to finish.
                    await asyncio.wait_for(started.wait(), 5)
                    server.handle_exit.assert_not_called()
                    # Second signal during the hook: force-exit straight
                    # through uvicorn (a bare handle_exit would only set
                    # should_exit — a third press would be needed).
                    hooked(2, None)  # SIGINT
                    assert server.force_exit is True
                    server.handle_exit.assert_called_once_with(2, None)
                    mock_set.assert_called()
                    # Hook completing hands the exit to uvicorn (again —
                    # one-shot guard means only the callback fires).
                    server.handle_exit.reset_mock()
                    release.set()
                    for _ in range(5):
                        await asyncio.sleep(0)
                    server.handle_exit.assert_called_once_with(15, None)
                    assert lc._shutdown_tasks == set()

    async def test_hook_exception_logged_and_exit_still_fires(self, app_state):
        """A raising hook is logged by the done-callback (not swallowed
        into an unretrieved-task warning) and uvicorn's exit still
        starts — the process never hangs on a failed hook."""
        import types as types_mod

        app_state = _make_app_state()
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(),
            _captured_signals=[],
            force_exit=False,
        )
        lc = app_state.state.lifecycle

        async def exploding_hook(*, signal_num):
            raise RuntimeError("notify exploded")

        with (
            patch.object(lc, "graceful_shutdown", side_effect=exploding_hook),
            patch("klangk.main.logger.error") as mock_log,
        ):
            hooked = None

            def grab(sig, handler):
                nonlocal hooked
                hooked = handler
                return MagicMock()

            with patch("signal.signal", side_effect=grab):
                with cls.capture_signals(server):
                    hooked(15, None)
                    for _ in range(5):
                        await asyncio.sleep(0)
        server.handle_exit.assert_called_once_with(15, None)
        assert any(
            "graceful-shutdown hook failed" in str(c)
            for c in mock_log.call_args_list
        )

    async def test_no_lifecycle_calls_original_directly_and_force_exits(
        self, app_state
    ):
        """Without a lifecycle on app.state (early boot crash window),
        every signal takes the force-exit path (a bare handle_exit would
        only set should_exit — a third press would be needed)."""
        import types as types_mod

        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace()
        )
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(), _captured_signals=[]
        )
        hooked = None

        def grab(sig, handler):
            nonlocal hooked
            hooked = handler
            return MagicMock()

        with patch("signal.signal", side_effect=grab):
            with cls.capture_signals(server):
                hooked(15, None)
        server.handle_exit.assert_called_once()

    async def test_non_main_thread_is_passthrough(self, app_state):
        """Off the main thread uvicorn (and we) install no handlers —
        the context manager yields without touching signal.signal."""
        import types as types_mod

        app_state = _make_app_state()
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(), _captured_signals=[]
        )

        calls = []

        # Run the context manager from a real non-main thread: the
        # main-thread guard yields without installing handlers.
        import threading

        err = []

        def off_main():
            try:
                with patch(
                    "signal.signal", side_effect=lambda *a: calls.append(a)
                ):
                    with cls.capture_signals(server):
                        pass
            except Exception as exc:  # pragma: no cover - guard
                err.append(exc)

        t = threading.Thread(target=off_main)
        t.start()
        t.join()
        assert err == []
        assert calls == []  # no handlers installed off-main-thread

    async def test_no_running_loop_calls_original(self, app_state):
        """A signal arriving with no event loop (early boot window)
        skips the async hook and hands the exit straight to uvicorn."""
        import types as types_mod

        app_state = _make_app_state()
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(), _captured_signals=[]
        )
        hooked = None

        def grab(sig, handler):
            nonlocal hooked
            hooked = handler
            return MagicMock()

        async def no_loop():
            # get_running_loop raises outside a running loop; simulate by
            # patching it, since we must run inside a loop to drive the
            # synchronous handler.
            with patch(
                "klangk.main.asyncio.get_running_loop",
                side_effect=RuntimeError,
            ):
                with patch("signal.signal", side_effect=grab):
                    with cls.capture_signals(server):
                        hooked(15, None)
            server.handle_exit.assert_called_once()

        await no_loop()
        # shutting_down was NOT set (the hook never ran).
        assert app_state.state.lifecycle.shutting_down is False

    async def test_captured_signals_reraised_on_exit(self, app_state):
        """Leaving capture_signals re-raises captured signals exactly as
        uvicorn does (so a supervisor's default disposition applies)."""
        import types as types_mod

        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace()
        )
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(),
            _captured_signals=[15],
        )
        raised = []
        with (
            patch(
                "signal.raise_signal", side_effect=lambda s: raised.append(s)
            ),
            patch("signal.signal", return_value=MagicMock()),
        ):
            with cls.capture_signals(server):
                pass
        assert raised == [15]


class TestConfigErrorExitStatus:
    """Config-error exit-status translation (#2666).

    The launcher turns uvicorn's generic STARTUP_FAILURE exit (3) into
    ``EX_CONFIG`` (78) when the lifespan flagged a deterministic
    ``ConfigurationError`` on ``app.state.startup_config_error``, so a
    supervisor can stop restart-looping a config that cannot fix itself.
    """

    def test_flagged_config_error_maps_to_ex_config(self):
        app_state = types.SimpleNamespace(
            startup_config_error=(
                "KLANGKD_DEFAULT_PASSWORD violates the configured "
                "password policy"
            )
        )
        assert main.config_error_exit_status(app_state) == EX_CONFIG

    def test_unflagged_state_maps_to_none(self):
        # No attribute at all (normal startup, non-config crash).
        assert main.config_error_exit_status(types.SimpleNamespace()) is None

    def test_none_flag_maps_to_none(self):
        app_state = types.SimpleNamespace(startup_config_error=None)
        assert main.config_error_exit_status(app_state) is None

    def test_ex_config_is_78(self):
        """sysexits.h EX_CONFIG — the value systemd configs will pin via
        ``RestartPreventExitStatus=78``."""
        assert EX_CONFIG == 78


class TestStartupShutdownRestart:
    async def test_startup_calls_container_sequence(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "prewarm_podman",
                new_callable=AsyncMock,
            ) as mock_prewarm,
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "start_health_loop"),
            patch.object(
                workspaces.Workspaces,
                "auto_start_workspaces",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            await app_state.state.lifecycle.startup()
        mock_prewarm.assert_awaited_once()
        mock_adopt.assert_awaited_once()

    async def test_startup_logs_auto_started_count(self):
        """:if n: arm — a nonzero auto-start count is logged, not skipped."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        with (
            patch.object(
                registry,
                "prewarm_podman",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "start_cleanup_loop",
            ),
            patch.object(
                registry,
                "start_health_loop",
            ),
            patch.object(
                registry,
                "start_crash_loop",
            ),
            patch.object(
                workspaces.Workspaces,
                "auto_start_workspaces",
                new_callable=AsyncMock,
                return_value=3,
            ),
        ):
            await app_state.state.lifecycle.startup()

    def test_recycle_request_without_loop_returns(self):
        """Sync-context call (no running loop, e.g. a signal handler after
        loop teardown): the guards return instead of raising."""
        import klangk.lifecycle as lifecycle_mod

        lc = lifecycle_mod.Lifecycle.__new__(lifecycle_mod.Lifecycle)
        lc.shutting_down = False
        lc._recycle_tasks = set()
        lc.request_recycle(source="test")  # guard returns, no task made
        assert not lc._recycle_tasks

        failed = MagicMock()
        failed.cancelled.return_value = False
        failed.exception.return_value = RuntimeError("recycle broke")
        lc._on_recycle_task_done(failed)  # recovery guard: quiet return
        assert not lc._recycle_tasks


class TestNoCoverAudit2910:
    """WS endpoint closures, gnubin PATH fixup, pid/port collision
    helpers, and the typer entry callback."""

    def _ws_endpoint(self, path):
        app = main.build_app(make_settings())
        for route in app.routes:
            if getattr(route, "path", None) == path and hasattr(
                route, "endpoint"
            ):
                return route.endpoint
        raise AssertionError(f"no ws route at {path}")

    async def test_ws_endpoint_delegates_to_handler(self):
        ws = MagicMock()
        with patch.object(
            main, "handle_websocket", new_callable=AsyncMock
        ) as handler:
            await self._ws_endpoint("/ws")(ws)
        handler.assert_awaited_once()

    async def test_consent_decider_endpoint_delegates(self):
        ws = MagicMock()
        with patch.object(
            main, "handle_consent_decider", new_callable=AsyncMock
        ) as handler:
            await self._ws_endpoint("/ws/consent-decider")(ws)
        handler.assert_awaited_once()

    async def test_egress_sidecar_endpoint_delegates(self):
        ws = MagicMock()
        with patch.object(
            main, "handle_egress_sidecar", new_callable=AsyncMock
        ) as handler:
            await self._ws_endpoint("/ws/egress-sidecar")(ws)
        handler.assert_awaited_once()

    async def test_ws_fallback_closes_with_4044(self):
        ws = AsyncMock()
        await self._ws_endpoint("/{path:path}")(ws, "junk")
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once()
        assert ws.close.await_args.kwargs["code"] == 4044

    def test_prepend_gnubin_paths_noop_on_linux(self):
        with patch.object(main.platform, "system", return_value="Linux"):
            main.prepend_gnubin_paths()  # early return, PATH untouched

    def test_prepend_gnubin_paths_no_brew(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        with (
            patch.object(main.platform, "system", return_value="Darwin"),
            patch.object(main.shutil, "which", return_value=None),
        ):
            main.prepend_gnubin_paths()
        assert os.environ["PATH"] == "/usr/bin"

    def test_prepend_gnubin_paths_prefix_probe_fails(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        with (
            patch.object(main.platform, "system", return_value="Darwin"),
            patch.object(
                main.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ),
            patch.object(
                main.subprocess,
                "run",
                side_effect=main.subprocess.TimeoutExpired(
                    cmd="brew", timeout=5
                ),
            ),
        ):
            main.prepend_gnubin_paths()
        assert os.environ["PATH"] == "/usr/bin"

    def test_prepend_gnubin_paths_empty_prefix(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        result = MagicMock(stdout="\n")
        with (
            patch.object(main.platform, "system", return_value="Darwin"),
            patch.object(
                main.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ),
            patch.object(main.subprocess, "run", return_value=result),
        ):
            main.prepend_gnubin_paths()
        assert os.environ["PATH"] == "/usr/bin"

    def test_prepend_gnubin_paths_prepends_dirs(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        result = MagicMock(stdout="/opt/homebrew\n")
        with (
            patch.object(main.platform, "system", return_value="Darwin"),
            patch.object(
                main.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ),
            patch.object(main.subprocess, "run", return_value=result),
            patch.object(main.os.path, "isdir", return_value=True),
        ):
            main.prepend_gnubin_paths()
        assert os.environ["PATH"].startswith(
            "/opt/homebrew/opt/coreutils/libexec/gnubin"
        )
        assert os.environ["PATH"].endswith(":/usr/bin")

    def test_report_pid_collision_first_report_then_dedup(self, tmp_path):
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
            }
        )
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "instance-id").write_text("inst-1\n")
        marker = main.refusal_marker_path(settings)
        marker.unlink(missing_ok=True)
        main._report_pid_collision(settings, 4242)  # logs + marks
        assert marker.exists()
        main._report_pid_collision(settings, 4242)  # dedup: quiet

    def test_check_port_collisions_exits_on_live_listener(self):
        settings = types.SimpleNamespace(
            port="5999",
            egress_port=None,
            listen="127.0.0.1",
            egress_listen="127.0.0.1",
        )
        with patch.object(main, "check_port_preflight", return_value=True):
            with pytest.raises(SystemExit) as caught:
                main._check_port_collisions(settings)
        assert caught.value.code == 1

    def test_check_port_collisions_passes_when_free(self):
        settings = types.SimpleNamespace(
            port="5999",
            egress_port="5998",
            listen="127.0.0.1",
            egress_listen="127.0.0.1",
        )
        with patch.object(main, "check_port_preflight", return_value=False):
            main._check_port_collisions(settings)  # must not raise


class TestMainEntryCallback2910:
    """The klangkd typer callback: deferral, startup wiring, failure
    translations (uvicorn mocked; nothing binds or serves)."""

    def _invoke(self, patched, args=()):
        from typer.testing import CliRunner

        with patched:
            return CliRunner().invoke(main.app, list(args))

    def test_subcommand_defers_startup(self):
        with patch.object(
            main, "resolve_config_path", return_value="/dev/null"
        ) as resolve:
            result = self._invoke(
                patch("klangk.doctor.doctor_main", return_value=0),
                ["doctor"],
            )
        resolve.assert_not_called()
        assert result.exit_code == 0

    def test_pid_collision_exits_1(self):
        settings = make_settings()
        with (
            patch.object(main, "resolve_config_path", return_value=None),
            patch.object(main, "KlangkSettings", return_value=settings),
            patch.object(main, "check_pid_preflight", return_value=4242),
            patch.object(main, "_report_pid_collision") as report,
            patch.object(main, "_check_port_collisions"),
        ):
            result = self._invoke(self._noop())
        report.assert_called_once()
        assert result.exit_code == 1

    def test_bind_oserror_exits_1(self, tmp_path):
        settings = make_settings({"KLANGKD_STATE_DIR": str(tmp_path)})
        server = MagicMock()
        server.run = MagicMock(side_effect=OSError("EADDRINUSE"))
        with (
            patch.object(main, "resolve_config_path", return_value=None),
            patch.object(main, "KlangkSettings", return_value=settings),
            patch.object(main, "check_pid_preflight", return_value=None),
            patch.object(main, "_check_port_collisions"),
            patch.object(main, "build_app") as build,
            patch.object(
                main,
                "make_graceful_exit_server",
                return_value=lambda cfg: server,
            ),
        ):
            built = MagicMock()
            built.state.util.set_uds_mode = MagicMock()
            build.return_value = built
            result = self._invoke(self._noop())
        assert result.exit_code == 1

    def test_uvicorn_config_error_maps_to_ex_config(self):
        settings = make_settings()
        server = MagicMock()
        server.run = MagicMock(side_effect=SystemExit(3))
        with (
            patch.object(main, "resolve_config_path", return_value=None),
            patch.object(main, "KlangkSettings", return_value=settings),
            patch.object(main, "check_pid_preflight", return_value=None),
            patch.object(main, "_check_port_collisions"),
            patch.object(main, "build_app") as build,
            patch.object(
                main,
                "make_graceful_exit_server",
                return_value=lambda cfg: server,
            ),
            patch.object(
                main,
                "config_error_exit_status",
                return_value=78,
            ),
        ):
            built = MagicMock()
            built.state.util.set_uds_mode = MagicMock()
            build.return_value = built
            result = self._invoke(self._noop())
        assert result.exit_code == 78

    @staticmethod
    def _noop():
        return patch.object(main, "prepend_gnubin_paths")

    def test_happy_path_runs_server(self):
        settings = make_settings()
        server = MagicMock()
        with (
            patch.object(main, "resolve_config_path", return_value=None),
            patch.object(main, "KlangkSettings", return_value=settings),
            patch.object(main, "check_pid_preflight", return_value=None),
            patch.object(main, "_check_port_collisions"),
            patch.object(main, "build_app") as build,
            patch.object(
                main,
                "make_graceful_exit_server",
                return_value=lambda cfg: server,
            ),
        ):
            built = MagicMock()
            built.state.util.set_uds_mode = MagicMock()
            build.return_value = built
            result = self._invoke(patch.object(main, "prepend_gnubin_paths"))
        assert result.exit_code == 0
        server.run.assert_called_once()
        built.state.util.set_uds_mode.assert_called_once_with(True)

    async def test_runtime_shutdown_tears_down_layers(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        with (
            patch(
                "klangk.lifecycle.wshandler.disconnect_all_websockets",
                new_callable=AsyncMock,
            ) as mock_disc,
            patch.object(
                registry, "shutdown", new_callable=AsyncMock
            ) as mock_shutdown,
        ):
            await app_state.state.lifecycle.runtime_shutdown()
        mock_disc.assert_awaited_once()
        mock_shutdown.assert_awaited_once()

    async def test_process_shutdown_disposes(self, app_state):
        app_state = _make_app_state()
        app_state.state.db = AsyncMock()
        with (
            patch.object(util_mod.Util, "remove_pid_file") as mock_remove,
        ):
            await app_state.state.lifecycle.process_shutdown()
        mock_remove.assert_called_once()
        app_state.state.db.dispose_engine.assert_awaited_once()

    async def test_recycle_runtime_runs_shutdown_then_startup(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None  # force fresh lock creation
        with (
            patch.object(
                lc, "runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch.object(lc, "startup", new_callable=AsyncMock) as mock_up,
        ):
            await lc.recycle_runtime()
        mock_down.assert_awaited_once()
        mock_up.assert_awaited_once()
        # Lock was created and is now held-free.
        assert lc._recycle_lock is not None

    async def test_recycle_runtime_reuses_existing_lock(self, app_state):
        # Seed a lock explicitly; ``recycle_runtime`` must reuse it rather
        # than create a new one. The lock is now per-instance (#1571), so a
        # fresh Lifecycle starts at the pre-first-use floor without a
        # cross-test reset fixture.
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = asyncio.Lock()
        existing = lc._recycle_lock
        with (
            patch.object(lc, "runtime_shutdown", new_callable=AsyncMock),
            patch.object(lc, "startup", new_callable=AsyncMock),
        ):
            await lc.recycle_runtime()
        # Same lock object reused, not replaced.
        assert lc._recycle_lock is existing

    async def test_recycle_lock_serializes_concurrent_calls(self, app_state):
        """Two restarts kicked off together run strictly one-after-another."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        order = []

        async def fake_shutdown():
            order.append("down-start")
            await asyncio.sleep(0.01)
            order.append("down-end")

        async def fake_startup():
            order.append("up")

        with (
            patch.object(lc, "runtime_shutdown", side_effect=fake_shutdown),
            patch.object(lc, "startup", side_effect=fake_startup),
        ):
            await asyncio.gather(
                lc.recycle_runtime(),
                lc.recycle_runtime(),
            )
        # Two complete down-start...down-end...up cycles, never interleaved.
        assert order == [
            "down-start",
            "down-end",
            "up",
            "down-start",
            "down-end",
            "up",
        ]

    async def test_restart_denies_on_invalid_config(self, app_state):
        """Invalid config denies the restart; no teardown, no startup."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(None, "bad config"),
            ) as mock_reload,
            patch.object(
                lc, "runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch.object(lc, "startup", new_callable=AsyncMock) as mock_up,
        ):
            await lc.recycle_runtime()
        mock_reload.assert_called_once()
        mock_down.assert_not_awaited()
        mock_up.assert_not_awaited()

    async def test_restart_reloads_then_applies_then_restarts(self, app_state):
        """Valid config: reload → apply → shutdown → startup."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        order = []
        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(new_settings, None),
            ),
            patch.object(
                lc,
                "apply_reloaded_settings",
                new_callable=AsyncMock,
                side_effect=lambda s: order.append("apply"),
            ),
            patch.object(
                lc,
                "runtime_shutdown",
                new_callable=AsyncMock,
                side_effect=lambda: order.append("shutdown"),
            ),
            patch.object(
                lc,
                "startup",
                new_callable=AsyncMock,
                side_effect=lambda: order.append("startup"),
            ),
        ):
            await lc.recycle_runtime()
        assert order == ["apply", "shutdown", "startup"]

    async def test_restart_graceful_sequence(self, app_state):
        """The full graceful-restart sequence, in order (#2527):
        broadcast draining → refuse starts → quiesce → drain containers
        → apply config → broadcast restarting → recycle → clear the
        drain flag before startup → broadcast host_started."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        order = []
        with (
            patch.object(
                lc, "reload_settings", return_value=(new_settings, None)
            ),
            patch.object(
                app_state.state.sockets,
                "notify_server_recycle",
                side_effect=lambda phase: order.append(f"notify:{phase}"),
            ),
            patch.object(
                app_state.state.sockets,
                "notify_host_started",
                side_effect=lambda: order.append("notify:started"),
            ),
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                side_effect=lambda **kw: (
                    order.append("drain:" + kw.get("reason", "")) or 2
                ),
            ) as mock_drain,
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_wait,
            patch.object(
                lc,
                "apply_reloaded_settings",
                new_callable=AsyncMock,
                side_effect=lambda s: order.append("apply"),
            ),
            patch.object(
                lc,
                "runtime_shutdown",
                new_callable=AsyncMock,
                side_effect=lambda: order.append(
                    f"shutdown(draining={registry.draining})"
                ),
            ),
            patch.object(
                lc,
                "startup",
                new_callable=AsyncMock,
                side_effect=lambda: order.append("startup"),
            ),
        ):
            await lc.recycle_runtime()
        assert order == [
            "notify:draining",
            "drain:server recycle",
            "apply",
            "notify:recycling",
            "shutdown(draining=True)",
            "startup",
            "notify:started",
        ]
        mock_drain.assert_awaited_once_with(reason="server recycle")
        # The quiesce timeout comes from the RELOADED settings, so a
        # change takes effect on this restart (#2527 review).
        mock_wait.assert_awaited_once_with(new_settings.quiesce_timeout)
        # The flag never survives the restart.
        assert registry.draining is False

    async def test_restart_keeps_drain_flag_through_startup(self, app_state):
        """The drain flag is NOT cleared before startup() — startup clears
        it itself after the container reaps, so a client that reconnects
        and starts a workspace during the prewarm/reap window is refused
        instead of having its container destroyed by the reap (#2527
        review)."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry
        seen = {}

        async def fake_shutdown():
            seen["at_shutdown"] = registry.draining

        async def fake_startup():
            seen["at_startup"] = registry.draining

        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(
                    make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"}),
                    None,
                ),
            ),
            patch.object(lc, "runtime_shutdown", side_effect=fake_shutdown),
            patch.object(lc, "startup", side_effect=fake_startup),
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                lc, "apply_reloaded_settings", new_callable=AsyncMock
            ),
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await lc.recycle_runtime()
        assert seen["at_shutdown"] is True
        assert seen["at_startup"] is True  # still refusing during startup

    async def test_startup_clears_drain_after_reaps(self, app_state):
        """startup() clears the drain flag once its container reaps are
        done (the window where a fresh client container could be reaped
        closes), and auto-start then runs with starts allowed."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        registry = app_state.state.container_registry
        order = []

        async def fake_reap(*args, **kwargs):
            order.append("reap")
            order.append(f"draining={registry.draining}")

        with (
            patch.object(registry, "prewarm_podman", new_callable=AsyncMock),
            patch.object(
                registry,
                "reap_instance_containers",
                side_effect=fake_reap,
            ),
            patch.object(
                registry,
                "reap_dead_owner_containers",
                side_effect=fake_reap,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "start_health_loop"),
            patch.object(registry, "start_crash_loop"),
            patch.object(
                app_state.state.workspaces,
                "auto_start_workspaces",
                new_callable=AsyncMock,
                side_effect=lambda: (
                    order.append(f"autostart(draining={registry.draining})")
                    or 0
                ),
            ),
        ):
            registry.draining = True
            await lc.startup()
        assert order == [
            "reap",
            "draining=True",
            "reap",
            "draining=True",
            "autostart(draining=False)",
        ]

    async def test_restart_quiesce_timeout_proceeds(self, app_state, caplog):
        """Straggler requests at timeout expiry are logged; the restart
        proceeds anyway."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        app_state.state.inflight_requests.increment()
        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(
                    make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"}),
                    None,
                ),
            ),
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                app_state.state.container_registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                lc, "apply_reloaded_settings", new_callable=AsyncMock
            ),
            patch.object(lc, "runtime_shutdown", new_callable=AsyncMock),
            patch.object(lc, "startup", new_callable=AsyncMock),
        ):
            with caplog.at_level("WARNING"):
                await lc.recycle_runtime()
        assert any(
            "still in flight" in r.message and r.levelname == "WARNING"
            for r in caplog.records
        )
        assert app_state.state.container_registry.draining is False

    async def test_restart_failure_clears_draining(self, app_state):
        """A mid-restart exception must not leave the node refusing new
        starts (the in-memory flag has no DB persistence to clear)."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry
        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(
                    make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"}),
                    None,
                ),
            ),
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("podman exploded"),
            ),
        ):
            with pytest.raises(RuntimeError, match="podman exploded"):
                await lc.recycle_runtime()
        assert registry.draining is False

    async def test_restart_aborts_when_shutdown_arrives_mid_drain(
        self, app_state
    ):
        """#2527 review: a TERM landing while the restart's drain is in
        flight aborts the restart after the drain — no settings apply,
        no runtime recycle — and never lifts the shutdown's drain flag
        (no auto-start resurrecting drained containers, no 503-lift
        while exiting)."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry
        order = []

        async def fake_drain(**kw):
            order.append("drain")
            # The shutdown lands mid-drain.
            lc.shutting_down = True
            return 1

        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(
                    make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"}),
                    None,
                ),
            ),
            patch.object(
                registry, "drain_all_containers", side_effect=fake_drain
            ),
            patch.object(
                lc, "apply_reloaded_settings", new_callable=AsyncMock
            ) as mock_apply,
            patch.object(
                lc, "runtime_shutdown", new_callable=AsyncMock
            ) as mock_down,
            patch.object(lc, "startup", new_callable=AsyncMock) as mock_up,
            patch.object(app_state.state.sockets, "notify_server_recycle"),
        ):
            await lc.recycle_runtime()
        assert order == ["drain"]
        mock_apply.assert_not_awaited()  # no config apply during teardown
        mock_down.assert_not_awaited()  # no runtime recycle
        mock_up.assert_not_awaited()  # no auto-start resurrect
        # The shutdown's flag was NOT lifted.
        assert registry.draining is True

    async def test_restart_aborts_before_starting_when_shutdown_precedes(
        self, app_state
    ):
        """A shutdown that won the race before the restart began (the
        on_sighup check-to-started window) aborts immediately with
        nothing touched."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        lc.shutting_down = True
        with (
            patch.object(lc, "reload_settings") as mock_reload,
            patch.object(lc, "runtime_shutdown", new_callable=AsyncMock),
        ):
            await lc.recycle_runtime()
        mock_reload.assert_not_called()

    async def test_recovery_skipped_during_shutdown(self, app_state):
        """Error recovery never resurrects a runtime mid-teardown."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc.shutting_down = True
        registry = app_state.state.container_registry
        registry.draining = True  # the shutdown owns it
        with patch.object(lc, "startup", new_callable=AsyncMock) as mock_up:
            await lc._recover_failed_recycle()
        mock_up.assert_not_awaited()
        assert registry.draining is True  # not lifted

    async def test_startup_does_not_clear_shutdown_drain_flag(self, app_state):
        """startup() clears the drain flag only when no shutdown owns
        it (the TERM path never runs startup(), but a restart racing a
        shutdown must not lift the refusal)."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        registry = app_state.state.container_registry
        lc.shutting_down = True
        registry.draining = True
        with (
            patch.object(registry, "prewarm_podman", new_callable=AsyncMock),
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "start_health_loop"),
            patch.object(registry, "start_crash_loop"),
            patch.object(
                app_state.state.workspaces,
                "auto_start_workspaces",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            await lc.startup()
        assert registry.draining is True  # shutdown keeps the refusal

    async def test_restart_denied_leaves_drain_untouched(self, app_state):
        """The deny path never broadcasts, never sets the drain flag."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry
        with (
            patch.object(
                lc, "reload_settings", return_value=(None, "bad config")
            ),
            patch.object(
                app_state.state.sockets, "notify_server_recycle"
            ) as mock_notify,
        ):
            await lc.recycle_runtime()
        mock_notify.assert_not_called()
        assert registry.draining is False

    def test_reload_settings_returns_new_when_valid(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new, error = lc.reload_settings()
        assert new is not None
        assert error is None
        assert new is not app_state.state.settings

    def test_reload_settings_returns_error_when_invalid(self, app_state):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        # Pydantic models can't be patched with patch.object; patch the
        # class method instead.
        with patch.object(
            type(app_state.state.settings),
            "reload",
            side_effect=ValueError("bad"),
        ):
            new, error = lc.reload_settings()
        assert new is None
        assert "bad" in error

    async def test_apply_reloaded_settings_calls_reconfigure(self, app_state):
        """Swap + reconfigure called on every subsystem."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        old_settings = app_state.state.settings
        called = []
        for attr in (
            "ssl_trust",
            "auth",
            "podman",
            "sockets",
            "container_registry",
            "proxy_watchdog",
            "terminal",
            "oidc",
            "features",
            "workspaces",
            "files",
            "db",
            "model",
            "acl",
            "email",
            "util",
            "lifecycle",
        ):
            obj = getattr(app_state.state, attr)
            orig = obj.reconfigure

            def make_tracker(name, orig_fn):
                def tracked(app):
                    called.append(name)
                    return orig_fn(app)

                return tracked

            obj.reconfigure = make_tracker(attr, orig)
        with patch.object(
            lc, "apply_pending_reseed", new_callable=AsyncMock
        ) as mock_reseed:
            await lc.apply_reloaded_settings(new_settings)
        assert app_state.state.settings is new_settings
        assert app_state.state.settings is not old_settings
        assert "ssl_trust" in called
        assert "oidc" in called
        assert "features" in called
        assert len(called) == 17
        mock_reseed.assert_awaited_once()

    async def test_apply_reloaded_settings_calls_caddy_reload(self, app_state):
        """When the proxy watchdog is the Caddy engine, apply_reloaded_settings
        calls its apply_pending_reload (#1559: a settings change is a fresh
        POST /load). The nginx engine has no apply_pending_reload and is skipped."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        # Stand in a Caddy-shaped watchdog with a flagging reconfigure +
        # an apply_pending_reload awaitable we can assert on.
        reload_calls = []

        class _FakeCaddyWd:
            def reconfigure(self, app):
                pass

            async def apply_pending_reload(self):
                reload_calls.append(1)

        app_state.state.proxy_watchdog = _FakeCaddyWd()
        with patch.object(lc, "apply_pending_reseed", new_callable=AsyncMock):
            await lc.apply_reloaded_settings(new_settings)
        assert reload_calls == [1]

    async def test_apply_reloaded_settings_swallows_caddy_reload_failure(
        self, app_state, caplog
    ):
        """A caddy apply_pending_reload failure is logged + skipped (doesn't
        abort the wider SIGHUP); Caddy keeps its last-known-good config."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})

        class _BadCaddyWd:
            def reconfigure(self, app):
                pass

            async def apply_pending_reload(self):
                raise RuntimeError("admin API down")

        app_state.state.proxy_watchdog = _BadCaddyWd()
        with (
            patch.object(lc, "apply_pending_reseed", new_callable=AsyncMock),
            caplog.at_level("WARNING"),
        ):
            await lc.apply_reloaded_settings(new_settings)  # must not raise
        assert "caddy config reload failed" in caplog.text

    async def test_apply_logs_warning_when_reconfigure_fails(
        self, app_state, caplog
    ):
        """A failing reconfigure is skipped + warned, the rest still run."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        new_settings = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        with (
            patch.object(
                app_state.state.ssl_trust,
                "reconfigure",
                side_effect=RuntimeError("ssl boom"),
            ),
            patch.object(
                app_state.state.oidc, "reconfigure"
            ) as mock_oidc_reconf,
            patch.object(
                lc, "apply_pending_reseed", new_callable=AsyncMock
            ) as mock_reseed,
            caplog.at_level("WARNING"),
        ):
            await lc.apply_reloaded_settings(new_settings)
        assert "ssl_trust reconfigure failed" in caplog.text
        mock_oidc_reconf.assert_called_once()
        mock_reseed.assert_awaited_once()

    def test_warn_non_reloadable_logs_changed_settings(
        self, app_state, caplog
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old = app_state.state.settings
        new = make_settings(
            {"KLANGKD_DEFAULT_PASSWORD": "test", "KLANGKD_PORT": "9999"}
        )
        with caplog.at_level("WARNING"):
            lc._warn_non_reloadable(old, new)
        assert "port" in caplog.text
        assert "full process restart" in caplog.text

    async def test_apply_pending_reseed_noop_without_flag(self, app_state):
        """apply_pending_reseed is a no-op when reconfigure hasn't been called."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with patch.object(
            lc, "seed_agent_user", new_callable=AsyncMock
        ) as mock_seed:
            await lc.apply_pending_reseed()
        mock_seed.assert_not_awaited()

    def test_warn_non_reloadable_silent_on_reloadable_only(
        self, app_state, caplog
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old = app_state.state.settings
        # Build new settings from the same env as old so only
        # reloadable fields differ.
        env = dict(old._reload_env)
        env["KLANGKD_FEATURES_ENABLE"] = "chat"  # reloadable (#1977)
        new = make_settings(env)
        with caplog.at_level("WARNING"):
            lc._warn_non_reloadable(old, new)
        assert "full process restart" not in caplog.text

    async def test_sighup_reseed_reconciles_to_fixed_identity(
        self, db, app_state, tmp_path
    ):
        """Acceptance test: SIGHUP re-seeds the agent to the FIXED identity
        (#2718) — a stale KLANGKWS_FEATURE_CHAT_AGENT_HANDLE setting in
        features_config: is ignored, and a pre-#2718 'klangk' row is
        renamed to 'klangk' without a process restart.

        Uses a REAL Features resolver (with a chat manifest) — not a mock —
        so it exercises the actual frontend_config() resolution from
        settings.features_config."""
        import json as json_mod

        from _helpers import get_test_db

        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        app_state.state.db = get_test_db()
        app_state.state.model = model.Model(app_state)

        # Stand up a chat manifest so the resolver knows the chat feature
        # (the identity keys are gone from the manifest; a stale one below
        # simulates an operator's un-pruned features_config).
        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        (frontend_dir / "features.json").write_text(
            json_mod.dumps(
                {
                    "features": [
                        {
                            "name": "chat",
                            "version": "1.0.0",
                            "description": "",
                            "config": {
                                "KLANGKWS_FEATURE_CHAT_AGENT_ENABLED": {
                                    "description": "",
                                    "default": "",
                                    "scope": "both",
                                }
                            },
                        }
                    ],
                    "defaults": [],
                    "container_env_keys": [],
                }
            )
        )
        app_state.state.settings.frontend_dir = str(frontend_dir)
        app_state.state.features = app_state.state.features.__class__(
            app_state
        )

        await lc.seed_agent_user()
        assert await app_state.state.model.users.agent_handle() == "klangk"

        # A pre-#2718 row (clanker era) + stale config keys.
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "UPDATE users SET handle = ?, email = ? WHERE id = ?",
                ("clanker", "clanker@example.com", model.AGENT_USER_ID),
            )
        app_state.state.model.users.clear_agent_cache()
        app_state.state.settings.features_config = {
            "KLANGKWS_FEATURE_CHAT_AGENT_HANDLE": "newbot",
            "KLANGKWS_FEATURE_CHAT_AGENT_EMAIL": "newbot@example.com",
        }
        lc.reconfigure(app_state)  # SIGHUP flags the re-seed
        await lc.apply_pending_reseed()
        # The fixed identity wins; the stale keys are ignored.
        assert await app_state.state.model.users.agent_handle() == "klangk"
        assert (
            await app_state.state.model.users.agent_email()
            == "klangk@example.com"
        )

    async def test_on_sighup_schedules_restart(self, app_state):
        """on_sighup creates a task that runs recycle_runtime."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with patch.object(
            lc, "recycle_runtime", new_callable=AsyncMock
        ) as mock_restart:
            lc.on_sighup()
            # Let the scheduled task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        mock_restart.assert_awaited_once()

    async def test_on_sighup_keeps_strong_task_reference(self, app_state):
        """The restart task is held in _recycle_tasks (an unreferenced
        task is GC-eligible mid-restart — the GC hazard the review
        flagged) and discarded when done."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with patch.object(lc, "recycle_runtime", new_callable=AsyncMock):
            lc.on_sighup()
            assert len(lc._recycle_tasks) == 1
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert lc._recycle_tasks == set()

    async def test_failed_restart_logs_and_recovers(self, app_state, caplog):
        """A restart that raises is logged and recovered: startup() is
        re-run, host_started is broadcast, the node never lingers
        half-restarted."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with (
            patch.object(
                lc,
                "recycle_runtime",
                new_callable=AsyncMock,
                side_effect=RuntimeError("drain exploded"),
            ),
            patch.object(
                lc, "startup", new_callable=AsyncMock
            ) as mock_startup,
            patch.object(
                app_state.state.sockets, "notify_host_started"
            ) as mock_started,
            caplog.at_level("ERROR"),
        ):
            lc.on_sighup()
            for _ in range(4):
                await asyncio.sleep(0)
        mock_startup.assert_awaited_once()
        mock_started.assert_called_once()
        assert any("recycle failed" in r.message for r in caplog.records)
        assert lc._recycle_tasks == set()

    async def test_failed_recovery_exits_process(self, app_state, caplog):
        """Recovery that also fails exits the process (code 1) so the
        service manager restarts us instead of a live zombie."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        with (
            patch.object(
                lc,
                "recycle_runtime",
                new_callable=AsyncMock,
                side_effect=RuntimeError("drain exploded"),
            ),
            patch.object(
                lc,
                "startup",
                new_callable=AsyncMock,
                side_effect=RuntimeError("startup also exploded"),
            ),
            patch("klangk.lifecycle.os._exit") as mock_exit,
            caplog.at_level("CRITICAL"),
        ):
            lc.on_sighup()
            for _ in range(6):
                await asyncio.sleep(0)
        mock_exit.assert_called_once_with(1)
        assert any("recovery failed" in r.message for r in caplog.records)

    async def test_cancelled_restart_task_logs_quietly(self, app_state):
        """A cancelled restart task (shutdown raced it) is not treated
        as a failure."""
        app_state = _make_app_state()
        lc = app_state.state.lifecycle

        async def hang():
            await asyncio.sleep(60)

        task = asyncio.ensure_future(hang())
        lc._recycle_tasks.add(task)
        task.add_done_callback(lc._on_recycle_task_done)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lc._recycle_tasks == set()

    async def test_lifespan_registers_sighup_handler(self, db, app_state):
        """The lifespan installs (and removes) a SIGHUP handler."""
        app = FastAPI()
        app_state = _make_app_state()
        app.state.container_registry = app_state.state.container_registry
        app.state.sockets = app_state.state.sockets
        app.state.settings = app_state.state.settings
        app.state.ssl_trust = app_state.state.ssl_trust
        app.state.db = app_state.state.db
        app.state.model = app_state.state.model
        app.state.proxy_watchdog = caddy_mod.CaddyWatchdog(app)
        app.state.consent_sweeper = consent.EgressConsentSweeper(app)
        app.state.inactivity_sweeper = inactivity.InactivitySweeper(app)
        app.state.memory_evictor = container.eviction.MemoryPressureEvictor(
            app
        )
        app.state.consent_deciders = consent.ConsentDeciderRegistry(app)
        app.state.consent_coordinator = consent.ConsentCoordinator(app)
        app.state.sidecar_connections = sidecar_connections.SidecarConnections(
            app
        )
        app.state.oidc = oidc.OIDC(app)
        app.state.features = features.Features(app)
        app.state.workspaces = workspaces.Workspaces(app)
        app.state.email = emailsvc_mod.EmailService(app)
        app.state.util = util_mod.Util(app)

        app.state.auth = auth_mod.Auth(app)
        app.state.lifecycle = app_state.state.lifecycle
        registry = app_state.state.container_registry
        loop = asyncio.get_running_loop()
        with (
            patch.object(
                loop, "add_signal_handler", new_callable=MagicMock
            ) as mock_add,
            patch.object(
                loop, "remove_signal_handler", new_callable=MagicMock
            ) as mock_remove,
            patch.object(
                registry,
                "reap_instance_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                registry,
                "reap_dead_owner_containers",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "start_cleanup_loop"),
            patch.object(registry, "shutdown", new_callable=AsyncMock),
            patch.object(util_mod.Util, "check_pid_file", return_value=None),
            patch.object(util_mod.Util, "write_pid_file"),
            patch.object(util_mod.Util, "remove_pid_file"),
        ):
            async with main.lifespan(app):
                mock_add.assert_called_once()
                # Handler is registered for SIGHUP pointing at on_sighup.
                registered_signal = mock_add.call_args.args[0]
                assert registered_signal == signal.SIGHUP
            mock_remove.assert_called_once_with(signal.SIGHUP)


# --- Static files ---


class TestSetupStaticFiles:
    async def test_mounts_static_files_and_adds_middleware(self, tmp_path):
        # Create a fake frontend directory with an index.html
        (tmp_path / "index.html").write_text("<html>hello</html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"hello" in resp.content

    async def test_no_cache_headers_on_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>hi</html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        # build_app registers this once; mirror it here (#2738).
        test_app.middleware("http")(main.no_cache_headers)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    async def test_no_cache_headers_on_js(self, tmp_path):
        (tmp_path / "app.js").write_text("console.log('hi')")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        test_app.middleware("http")(main.no_cache_headers)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/app.js")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )

    async def test_no_cache_headers_not_on_other_files(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        test_app.middleware("http")(main.no_cache_headers)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/image.png")
        assert "Cache-Control" not in resp.headers

    async def test_mounts_branding_from_data_dir(self, tmp_path):
        # When <data_dir>/branding exists, it is served at /branding.
        (tmp_path / "index.html").write_text("<html></html>")
        branding = tmp_path / "branding" / "logo.png"
        branding.parent.mkdir(parents=True, exist_ok=True)
        branding.write_bytes(b"\x89PNG\r\n\x1a\n")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/logo.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

    async def test_branding_prefers_customize_dir(self, tmp_path, monkeypatch):
        # When <KLANGKD_CUSTOMIZE_DIR>/branding exists, it is preferred
        # over <data_dir>/branding.  See #1360.
        (tmp_path / "index.html").write_text("<html></html>")
        custom = tmp_path / "cust"
        branding = custom / "branding"
        branding.mkdir(parents=True)

        test_app = FastAPI()
        _settings = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(custom)})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        branding_route = [
            r for r in test_app.routes if getattr(r, "path", "") == "/branding"
        ]
        assert branding_route
        assert Path(branding_route[0].app.directory) == branding

    async def test_branding_skipped_when_no_dir_exists(self, tmp_path):
        # When neither customize_dir/branding nor data_dir/branding
        # exists, the /branding mount is skipped entirely.
        (tmp_path / "index.html").write_text("<html></html>")

        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)

        branding_route = [
            r for r in test_app.routes if getattr(r, "path", "") == "/branding"
        ]
        assert not branding_route

    async def test_branding_mount_404_for_missing_file(self, tmp_path):
        (tmp_path / "index.html").write_text("<html></html>")
        (tmp_path / "branding").mkdir()
        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/nope.png")
        assert resp.status_code == 404

    async def test_branding_files_get_no_no_cache_header(self, tmp_path):
        # Logos should be cacheable; the no-cache middleware only targets
        # .html/.js/"/", not branding assets.
        (tmp_path / "index.html").write_text("<html></html>")
        branding = tmp_path / "branding" / "logo.png"
        branding.parent.mkdir(parents=True, exist_ok=True)
        branding.write_bytes(b"\x89PNG")
        test_app = FastAPI()
        _settings = make_settings({})
        test_app.state.settings = _settings
        test_app.state.util = util_mod.Util(test_app)

        test_app.state.auth = auth_mod.Auth(test_app)
        main.setup_static_files(test_app, tmp_path)
        test_app.middleware("http")(main.no_cache_headers)
        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/branding/logo.png")
        assert resp.status_code == 200
        assert "Cache-Control" not in resp.headers


# --- Logfire ---


class TestSetupLogfire:
    def test_no_token_returns_false(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        app = FastAPI()
        assert main.setup_logfire(app) is False

    def test_with_token_instruments_app(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.delenv("LOGFIRE_BASE_URL", raising=False)
        monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            result = main.setup_logfire(app)
        assert result is True
        mock_logfire.configure.assert_called_once_with()
        mock_logfire.instrument_fastapi.assert_called_once_with(app)

    def test_base_url_passed_via_advanced_options(self, monkeypatch):
        # LOGFIRE_BASE_URL must be passed as advanced=AdvancedOptions(base_url=...),
        # not as the deprecated top-level base_url= argument (#1410).
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://logfire.example.com")
        monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            result = main.setup_logfire(app)
        assert result is True
        mock_logfire.AdvancedOptions.assert_called_once_with(
            base_url="https://logfire.example.com"
        )
        mock_logfire.configure.assert_called_once()
        configure_kwargs = mock_logfire.configure.call_args.kwargs
        assert "advanced" in configure_kwargs
        assert (
            configure_kwargs["advanced"]
            is mock_logfire.AdvancedOptions.return_value
        )
        assert "base_url" not in configure_kwargs


class TestCorsOrigins:
    """Moved to test_util.py (Util.cors_origins, #1503)."""

    def test_with_base_url_and_environment(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://custom.logfire")
        monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            main.setup_logfire(app)
        mock_logfire.AdvancedOptions.assert_called_once_with(
            base_url="https://custom.logfire"
        )
        mock_logfire.configure.assert_called_once_with(
            advanced=mock_logfire.AdvancedOptions.return_value,
            environment="staging",
        )


# --- PID file helpers ---


class TestPidFile:
    """PID-file helpers are methods of ``Util`` (``app.state.util``) since
    #1565 — the PID file is part of instance identity, so read/write/check
    live on the same ``Util`` that owns the ID."""

    def _util_with_pid_file(self, monkeypatch, pid_file):
        """A Util whose ``pid_file_path()`` returns ``pid_file``.

        ``instance_id()`` is short-circuited so it never touches the
        filesystem (the real path would read ``<data_dir>/instance-id``)."""
        util = util_mod.Util(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        monkeypatch.setattr(util, "_instance_id", "iid")
        monkeypatch.setattr(util, "pid_file_path", lambda: pid_file)
        return util

    def test_check_pid_file_no_file(self, tmp_path, monkeypatch):
        util = self._util_with_pid_file(
            monkeypatch, tmp_path / "klangk-test.pid"
        )
        assert util.check_pid_file() is None

    def test_check_pid_file_stale_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID that (almost certainly) doesn't exist
        pid_file.write_text("2000000")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None
        assert not pid_file.exists()

    def test_check_pid_file_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text(str(os.getpid()))
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None

    def test_check_pid_file_live_pid_permission_error(
        self, tmp_path, monkeypatch
    ):
        pid_file = tmp_path / "klangk-test.pid"
        # PID 1 (init) is always alive
        pid_file.write_text("1")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        # os.kill(1, 0) raises PermissionError for non-root
        result = util.check_pid_file()
        assert result == 1

    def test_check_pid_file_live_foreign_pid(self, tmp_path, monkeypatch):
        """Live PID that os.kill(pid, 0) succeeds on (not our PID)."""
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID we know is alive and can signal (our parent process)
        ppid = os.getppid()
        pid_file.write_text(str(ppid))
        util = self._util_with_pid_file(monkeypatch, pid_file)
        result = util.check_pid_file()
        assert result == ppid

    def test_check_pid_file_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("not-a-number")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        assert util.check_pid_file() is None

    def test_write_and_remove_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        util = self._util_with_pid_file(monkeypatch, pid_file)
        util.write_pid_file()
        assert pid_file.read_text() == str(os.getpid())
        util.remove_pid_file()
        assert not pid_file.exists()

    def test_remove_pid_file_only_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("99999")
        util = self._util_with_pid_file(monkeypatch, pid_file)
        util.remove_pid_file()
        # File should still exist — not our PID
        assert pid_file.exists()

    def test_remove_pid_file_missing(self, tmp_path, monkeypatch):
        util = self._util_with_pid_file(
            monkeypatch, tmp_path / "klangk-test.pid"
        )
        # Should not raise
        util.remove_pid_file()

    async def test_pid_file_path_uses_state_dir(self, tmp_path):
        """pid_file_path() lives in state_dir and embeds the instance ID."""
        util = util_mod.Util(
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    settings=make_settings(
                        {"KLANGKD_STATE_DIR": str(tmp_path)}
                    )
                )
            )
        )
        monkeypatch_id = "12345678-1234-1234-1234-123456789abc"
        util._instance_id = monkeypatch_id
        path = util.pid_file_path()
        assert path.parent == tmp_path
        assert path.name == f"klangk-{monkeypatch_id}.pid"


class TestCheckPidPreflight:
    """Tests for main.check_pid_preflight (#1837)."""

    def test_no_instance_id_file(self, tmp_path):
        from klangk.main import check_pid_preflight

        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
            }
        )
        assert check_pid_preflight(settings) is None

    def test_empty_instance_id(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) is None

    def test_no_pid_file(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) is None

    def test_stale_pid(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        pid_file = state_dir / "klangk-test-id.pid"
        pid_file.write_text("2000000")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) is None
        assert not pid_file.exists()

    def test_own_pid(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "klangk-test-id.pid").write_text(str(os.getpid()))
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) is None

    def test_live_foreign_pid(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ppid = os.getppid()
        (state_dir / "klangk-test-id.pid").write_text(str(ppid))
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) == ppid

    def test_permission_error_pid(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "klangk-test-id.pid").write_text("1")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) == 1

    def test_invalid_pid_content(self, tmp_path):
        from klangk.main import check_pid_preflight

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("test-id")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "klangk-test-id.pid").write_text("not-a-number")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(state_dir),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert check_pid_preflight(settings) is None


class TestCheckPortPreflight:
    """Tests for launcher.check_port_preflight (#2211)."""

    def test_no_listener_returns_false(self):
        from klangk.main import check_port_preflight

        # Pick a high port unlikely to be in use.
        assert check_port_preflight("127.0.0.1", 59123) is False

    def test_live_listener_returns_true(self):
        import socket as _socket

        from klangk.main import check_port_preflight

        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert check_port_preflight("127.0.0.1", port) is True
        finally:
            srv.close()

    def test_connection_refused_returns_false(self):
        import socket as _socket

        from klangk.main import check_port_preflight

        # Bind then close — port is free but was recently used.
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        assert check_port_preflight("127.0.0.1", port) is False


class TestLauncherPidPreflightGracefulExit:
    """The launcher's ``main()`` must log and ``sys.exit(1)`` — not crash with
    ``ImportError`` — when a second instance collides with a running one (#1993).

    ``check_pid_preflight`` is unit-tested above; this exercises the *error
    path that consumes it* (inside ``main()``, which is otherwise
    ``# pragma: no cover``) by launching the real launcher in a subprocess
    against a state/data dir that already records a live foreign PID.
    """

    def test_second_instance_exits_cleanly(self, tmp_path):
        state_dir, data_dir = _plant_live_winner(tmp_path, os.getppid())
        result = _launch_refusal_subprocess(state_dir, data_dir)

        # Graceful refusal, not a Python traceback.
        assert result.returncode == 1, result.stderr
        assert "Another klangk instance" in result.stderr
        # The pre-fix bug crashed with ImportError before the message could
        # be logged (``from klangk.logger import logger`` — no such symbol).
        assert "ImportError" not in result.stderr


def _plant_live_winner(tmp_path, winner_pid):
    """Plant an instance-id + pidfile recording a live foreign winner PID.

    The launcher's pre-flight sees a live, non-self PID and refuses (#1837).
    Returns ``(state_dir, data_dir)``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "instance-id").write_text("test-id")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "klangk-test-id.pid").write_text(str(winner_pid))
    return state_dir, data_dir


def _launch_refusal_subprocess(state_dir, data_dir):
    """Run the real launcher in a subprocess against planted state/data dirs.

    Strips inherited ``KLANGKD_*`` so the subprocess sees only the planted
    dirs (env > file precedence; CI/dev shells carry ``KLANGKD_*`` that would
    otherwise override them).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("KLANGKD_")}
    env["KLANGKD_STATE_DIR"] = str(state_dir)
    env["KLANGKD_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "-m", "klangk.main", "--config=none"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestLauncherPidRefusalDedup:
    """#2021: a losing (second) process reports why it exits — but only the
    first time it collides with a given live winner PID. A service
    supervisor's restart loop of the loser must not spam one refusal line
    per retry; a *different* winner PID is reported fresh.

    The dedup is independent of whether stderr is a TTY: the discriminator is
    winner-vs-loser, not interactive-vs-supervised.
    """

    def test_retry_against_same_winner_is_silent(self, tmp_path):
        # First collision logs + records the winner PID.
        winner_pid = os.getppid()
        state_dir, data_dir = _plant_live_winner(tmp_path, winner_pid)
        first = _launch_refusal_subprocess(state_dir, data_dir)
        assert first.returncode == 1, first.stderr
        assert "Another klangk instance" in first.stderr

        # A supervisor restart against the SAME winner PID stays quiet: the
        # refusal was already reported for this PID. Still exits non-zero.
        second = _launch_refusal_subprocess(state_dir, data_dir)
        assert second.returncode == 1, second.stderr
        assert "Another klangk instance" not in second.stderr

    def test_new_winner_pid_logs_again(self, tmp_path):
        # The dedup marker is keyed on the winner PID, so a *new* live winner
        # is reported fresh rather than permanently silenced.
        state_dir, data_dir = _plant_live_winner(tmp_path, os.getppid())
        first = _launch_refusal_subprocess(state_dir, data_dir)
        assert "Another klangk instance" in first.stderr

        # A second, distinct live foreign PID (a child we keep alive).
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            (state_dir / "klangk-test-id.pid").write_text(str(sleeper.pid))
            second = _launch_refusal_subprocess(state_dir, data_dir)
            assert second.returncode == 1, second.stderr
            assert "Another klangk instance" in second.stderr
        finally:
            sleeper.terminate()
            sleeper.wait()

    def test_marker_file_is_written_sibling_of_pidfile(self, tmp_path):
        state_dir, data_dir = _plant_live_winner(tmp_path, os.getppid())
        _launch_refusal_subprocess(state_dir, data_dir)
        assert (state_dir / "klangk-test-id.refusal").exists()


class TestRefusalDedupHelpers:
    """Unit tests for the refusal-dedup helpers in main.py (#2021)."""

    def test_marker_path_none_without_instance_id(self, tmp_path):
        from klangk.main import refusal_marker_path

        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
            }
        )
        assert refusal_marker_path(settings) is None

    def test_marker_path_none_for_empty_instance_id(self, tmp_path):
        from klangk.main import refusal_marker_path

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert refusal_marker_path(settings) is None

    def test_marker_path_sibling_of_pidfile(self, tmp_path):
        from klangk.main import refusal_marker_path

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "instance-id").write_text("abc")
        settings = make_settings(
            {
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(data_dir),
            }
        )
        assert (
            refusal_marker_path(settings)
            == tmp_path / "state" / "klangk-abc.refusal"
        )

    def test_already_reported_false_when_absent(self, tmp_path):
        from klangk.main import refusal_already_reported

        assert not refusal_already_reported(tmp_path / "missing.refusal", 123)

    def test_already_reported_true_when_pid_matches(self, tmp_path):
        from klangk.main import (
            mark_refusal_reported,
            refusal_already_reported,
        )

        marker = tmp_path / "klangk-x.refusal"
        mark_refusal_reported(marker, 456)
        assert refusal_already_reported(marker, 456)

    def test_already_reported_false_when_pid_differs(self, tmp_path):
        from klangk.main import (
            mark_refusal_reported,
            refusal_already_reported,
        )

        marker = tmp_path / "klangk-x.refusal"
        mark_refusal_reported(marker, 456)
        assert not refusal_already_reported(marker, 789)

    def test_already_reported_false_on_corrupt_marker(self, tmp_path):
        from klangk.main import refusal_already_reported

        marker = tmp_path / "klangk-x.refusal"
        marker.write_text("not-a-pid")
        assert not refusal_already_reported(marker, 123)

    def test_mark_refusal_reported_creates_parent(self, tmp_path):
        from klangk.main import mark_refusal_reported

        marker = tmp_path / "nested" / "dir" / "klangk-x.refusal"
        mark_refusal_reported(marker, 999)
        assert marker.read_text() == "999"

    def test_mark_refusal_reported_swallows_oserror(self, tmp_path):
        # A marker whose parent is a regular file can't be created → OSError
        # (FileExistsError) from mkdir. Best-effort: must not raise, so the
        # refusal path still exits cleanly (worst case: one extra line next
        # retry).
        from klangk.main import mark_refusal_reported

        blocker = tmp_path / "blocker"
        blocker.write_text("")  # a file, not a directory
        marker = blocker / "klangk-x.refusal"
        mark_refusal_reported(marker, 999)  # must not raise
        assert not marker.exists()


class TestBuildApp:
    """Tests for build_app() composition root (#1426)."""

    def test_build_app_returns_fastapi(self):
        settings = make_settings({})
        app = main.build_app(settings)
        assert isinstance(app, FastAPI)

    def test_build_app_sets_state_settings(self):
        settings = make_settings({})
        app = main.build_app(settings)
        assert app.state.settings is settings

    def test_build_app_includes_routers(self):
        app = main.build_app(make_settings({}))
        paths = set(app.openapi()["paths"].keys())
        assert "/api/v1/config" in paths  # api router with prefix

    def test_build_app_has_ws_endpoint(self):
        app = main.build_app(make_settings({}))
        ws_paths = {
            r.path
            for r in app.routes
            if hasattr(r, "path") and r.path == "/ws"
        }
        assert "/ws" in ws_paths

    def test_build_app_has_ws_fallback(self, tmp_path):
        """#2322: a catch-all ws fallback is registered before the StaticFiles
        mount so unmatched WS upgrades get a clear close instead of the
        StaticFiles ``assert scope["type"] == "http"`` crash."""
        (tmp_path / "index.html").write_text("<html></html>")
        settings = make_settings({"KLANGKD_FRONTEND_DIR": str(tmp_path)})
        app = main.build_app(settings)
        fallback_idx = None
        static_idx = None
        for i, r in enumerate(app.routes):
            if getattr(r, "path", None) == "/{path:path}":
                fallback_idx = i
            if getattr(r, "name", None) == "frontend":
                static_idx = i
        assert fallback_idx is not None, "ws fallback route missing"
        assert static_idx is not None, "static mount missing"
        assert fallback_idx < static_idx, (
            "ws fallback must be registered before the StaticFiles mount"
        )

    def test_build_app_registers_exception_handlers(self):
        app = main.build_app(make_settings({}))
        assert model.AgentPrincipalError in app.exception_handlers

    def test_build_app_default_engine_is_caddy(self):
        """KLANGKD_PROXY_ENGINE unset → the Caddy CaddyWatchdog (#1559, #1634).

        Since #1634 the default is ``caddy``; nginx is a deprecated escape
        hatch selected explicitly.
        """
        from klangk.caddy import CaddyWatchdog

        app = main.build_app(make_settings({}))
        assert isinstance(app.state.proxy_watchdog, CaddyWatchdog)

    def test_build_app_warns_when_frontend_dir_absent(self, caplog):
        """build_app warns when frontend_dir doesn't exist (#1600).

        A packaged install whose wheel lacked the Flutter artifact, or a
        bad KLANGKD_FRONTEND_DIR override, must be obvious instead of
        silently serving an API-only app.
        """
        import logging

        settings = make_settings(
            {"KLANGKD_FRONTEND_DIR": "/nonexistent/klangk/frontend"}
        )
        with caplog.at_level(logging.WARNING, logger=main.logger.name):
            app = main.build_app(settings)
        assert isinstance(app, FastAPI)
        assert any(
            "will not be served" in rec.message for rec in caplog.records
        )
        # No "/" static mount was added (starlette names it "frontend").
        assert not [
            r for r in app.routes if getattr(r, "name", "") == "frontend"
        ]

    def test_build_app_mounts_frontend_when_dir_exists(self, tmp_path):
        """build_app mounts the UI when frontend_dir exists (#1600)."""
        (tmp_path / "index.html").write_text("<html></html>")
        settings = make_settings({"KLANGKD_FRONTEND_DIR": str(tmp_path)})
        app = main.build_app(settings)
        mounts = [
            r for r in app.routes if getattr(r, "name", "") == "frontend"
        ]
        assert mounts, "expected '/' static mount when frontend dir exists"

    def test_build_app_configures_logging_from_settings(self):
        """build_app re-applies the log level from KLANGKD_LOG_LEVEL (#1467).

        Logging is global module state (no app.state.logger object);
        build_app calls ``klangk.logger.configure(settings)`` after settings
        are finalized. Build an app with DEBUG and confirm the root level
        reflects it.
        """
        import logging

        app = main.build_app(make_settings({"KLANGKD_LOG_LEVEL": "DEBUG"}))
        assert app.state.settings.log_level == "DEBUG"
        assert logging.getLogger().level == logging.DEBUG

    def test_no_module_scope_basic_config(self):
        """Logging is not configured as an import side-effect (#1467).

        main.py must not call ``logging.basicConfig(...)`` at module scope —
        configuration lives in :mod:`klangk.logger` (applied at its import
        and re-applied via ``configure(settings)`` in build_app). Checked via
        AST so comments / docstrings that merely mention the name don't trip
        it.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(main))
        basic_config_calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "basicConfig"
        ]
        assert not basic_config_calls

    def test_module_app_is_the_typer_cli_not_an_asgi_app(self):
        """main.py exposes no module-level ASGI app (#1454, #2753).

        The composition root stays sealed: the module-level ``app`` is the
        *Typer CLI* (the ``klangkd`` console-script entry point, merged
        from launcher.py in #2753), never a pre-built FastAPI app. The
        ASGI app is built explicitly via ``build_app(settings)``; the E2E
        suites launch real ``klangkd`` (``python -m klangk.main``) and
        contact it over its UDS (#1525).
        """
        import typer

        assert isinstance(main.app, typer.Typer)
        assert not isinstance(main.app, FastAPI)
        # No pre-built ASGI app under any other conventional name either.
        assert not hasattr(main, "asgi_app")


class TestGetAppDep:
    """Tests for get_app_dep per-request bridge (#1426)."""

    def test_returns_app(self, app_state):
        settings = make_settings({})
        app = main.build_app(settings)
        request = MagicMock()
        request.app = app
        result = main.get_app_dep(request)
        assert result is app
        assert result.state.settings is settings


class TestLiveCORSMiddleware:
    """LiveCORSMiddleware reads origins from app state on each request (#1610)."""

    async def test_rebuilds_on_settings_change(self):
        settings1 = make_settings({"KLANGKD_CORS_ORIGINS": "http://a.example"})
        app = main.build_app(settings1)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.options(
                "/api/v1/version",
                headers={
                    "Origin": "http://a.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert (
                resp.headers.get("access-control-allow-origin")
                == "http://a.example"
            )

            # Swap settings with a different origin
            settings2 = make_settings(
                {"KLANGKD_CORS_ORIGINS": "http://b.example"}
            )
            app.state.settings = settings2

            resp2 = await client.options(
                "/api/v1/version",
                headers={
                    "Origin": "http://b.example",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert (
                resp2.headers.get("access-control-allow-origin")
                == "http://b.example"
            )

    async def test_caches_until_settings_change(self):
        import types as _types

        settings = make_settings({"KLANGKD_CORS_ORIGINS": "http://x.example"})
        app = _types.SimpleNamespace(
            state=_types.SimpleNamespace(
                settings=settings,
                util=util_mod.Util(
                    _types.SimpleNamespace(
                        state=_types.SimpleNamespace(settings=settings)
                    )
                ),
            )
        )
        live_cors = main.LiveCORSMiddleware(
            lambda scope, recv, send: None, fastapi_app=app
        )
        # First call builds the inner middleware
        inner1 = live_cors._rebuild_if_needed()
        inner2 = live_cors._rebuild_if_needed()
        assert inner1 is inner2  # cached — same settings object

        # Swap settings → inner changes
        settings2 = make_settings({"KLANGKD_CORS_ORIGINS": "http://y.example"})
        app.state.settings = settings2
        app.state.util = util_mod.Util(
            _types.SimpleNamespace(
                state=_types.SimpleNamespace(settings=settings2)
            )
        )
        inner3 = live_cors._rebuild_if_needed()
        assert inner3 is not inner1


class TestRemountFrontend:
    """Lifecycle.remount_frontend replaces the frontend mount (#1610)."""

    async def test_remount_swaps_static_dir(self, tmp_path, app_state):
        lc = main.Lifecycle(app_state)
        app = FastAPI()
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        (old_dir / "index.html").write_text("old")
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / "index.html").write_text("new")
        app.state.settings = make_settings({})
        app.state.util = util_mod.Util(app)
        main.setup_static_files(app, old_dir)

        new_settings = make_settings({"KLANGKD_FRONTEND_DIR": str(new_dir)})
        lc.remount_frontend(app, new_settings)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"new" in resp.content

    async def test_remount_removes_mount_when_dir_missing(
        self, tmp_path, app_state
    ):
        lc = main.Lifecycle(app_state)
        app = FastAPI()
        old_dir = tmp_path / "old"
        old_dir.mkdir()
        (old_dir / "index.html").write_text("old")
        app.state.settings = make_settings({})
        app.state.util = util_mod.Util(app)
        main.setup_static_files(app, old_dir)

        new_settings = make_settings(
            {"KLANGKD_FRONTEND_DIR": str(tmp_path / "nonexistent")}
        )
        lc.remount_frontend(app, new_settings)

        # The old mount should be gone — no routes named "frontend"
        frontend_routes = [
            r for r in app.routes if getattr(r, "name", None) == "frontend"
        ]
        assert frontend_routes == []

    async def test_apply_reloaded_settings_remounts_frontend(
        self, db, tmp_path
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old_settings = app_state.state.settings
        new_settings = make_settings(
            dict(old_settings._reload_env, KLANGKD_FRONTEND_DIR=str(tmp_path))
        )
        with patch.object(lc, "remount_frontend") as mock_remount:
            await lc.apply_reloaded_settings(new_settings)
        mock_remount.assert_called_once()

    async def test_apply_reloaded_settings_skips_remount_when_unchanged(
        self, db
    ):
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        old_settings = app_state.state.settings
        # Same frontend_dir → no remount
        new_settings = make_settings(dict(old_settings._reload_env))
        with patch.object(lc, "remount_frontend") as mock_remount:
            await lc.apply_reloaded_settings(new_settings)
        mock_remount.assert_not_called()


async def test_request_recycle_ignored_when_shutting_down(app_state, caplog):
    """#2661: a scheduled recycle (or SIGHUP) arriving after a shutdown
    began must not spawn a recycle task — the exit owns the process."""
    app_state = _make_app_state()
    lc = app_state.state.lifecycle
    lc.shutting_down = True
    with caplog.at_level("INFO"):
        lc.request_recycle(source="scheduled recycle")
    assert not lc._recycle_tasks
    assert any("recycle ignored" in r.message for r in caplog.records)


class TestLifecycleBranchGaps2834:
    """#2834 branch gate: the shutdown-owned drain flag and the
    no-caddy-watchdog apply path."""

    async def test_restart_during_shutdown_keeps_draining(self, app_state):
        # A shutdown owning the process when the restart's finally runs:
        # the drain flag must stay set (clearing it would lift the
        # shutdown's start-refusal while exiting, #2527 review).
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        lc._recycle_lock = None
        registry = app_state.state.container_registry

        async def _startup_sees_shutdown():
            # A shutdown lands after the abort checkpoints (during the
            # runtime recycle): the restart completes normally, and the
            # finally must NOT lift the shutdown's start-refusal.
            lc.shutting_down = True

        with (
            patch.object(
                lc,
                "reload_settings",
                return_value=(
                    make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"}),
                    None,
                ),
            ),
            patch.object(
                registry,
                "drain_all_containers",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                app_state.state.inflight_requests,
                "wait_for_idle",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                lc, "apply_reloaded_settings", new_callable=AsyncMock
            ),
            patch.object(lc, "runtime_shutdown", new_callable=AsyncMock),
            patch.object(lc, "startup", side_effect=_startup_sees_shutdown),
        ):
            await lc.recycle_runtime()
        assert registry.draining is True

    async def test_apply_reloaded_settings_without_caddy_watchdog(
        self, app_state
    ):
        # No caddy watchdog (nginx engine / proxy disabled) and an
        # unchanged frontend_dir: no reload applied, no remount.
        app_state = _make_app_state()
        lc = app_state.state.lifecycle
        app_state.state.proxy_watchdog = None
        new = make_settings({"KLANGKD_DEFAULT_PASSWORD": "test"})
        remount = MagicMock()
        with patch.object(lc, "remount_frontend", remount):
            await lc.apply_reloaded_settings(new)
        remount.assert_not_called()
        assert app_state.state.settings is new


class TestGracefulExitBranchGaps2834:
    def _server_cls(self, app_state):
        from klangk.main import make_graceful_exit_server

        return make_graceful_exit_server(app_state)

    async def test_cancelled_hook_task_still_hands_exit_to_uvicorn(
        self, app_state
    ):
        """#2834: a hook task CANCELLED before finishing must not hit
        Task.exception() (which raises for cancelled tasks) -- the
        done-callback skips the log and still calls uvicorn's exit."""
        import types as types_mod

        app_state = _make_app_state()
        cls = self._server_cls(app_state)
        server = types_mod.SimpleNamespace(
            handle_exit=MagicMock(),
            _captured_signals=[],
            force_exit=False,
        )
        lc = app_state.state.lifecycle
        started = asyncio.Event()

        async def slow_hook(*, signal_num):
            started.set()
            await asyncio.Event().wait()  # park until cancelled

        with patch.object(lc, "graceful_shutdown", side_effect=slow_hook):
            hooked = None

            def grab(sig, handler):
                nonlocal hooked
                hooked = handler
                return MagicMock()

            with patch("signal.signal", side_effect=grab):
                with cls.capture_signals(server):
                    hooked(15, None)
                    await asyncio.wait_for(started.wait(), 5)
                    task = next(iter(lc._shutdown_tasks))
                    task.cancel()
                    for _ in range(5):
                        await asyncio.sleep(0)
        server.handle_exit.assert_called_once_with(15, None)
        assert lc._shutdown_tasks == set()


class TestEntryCallbackEdgeArms2910:
    def test_prepend_gnubin_paths_without_gnubin_dirs(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        result = MagicMock(stdout="/opt/homebrew\n")
        with (
            patch.object(main.platform, "system", return_value="Darwin"),
            patch.object(
                main.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ),
            patch.object(main.subprocess, "run", return_value=result),
            patch.object(main.os.path, "isdir", return_value=False),
        ):
            main.prepend_gnubin_paths()
        assert os.environ["PATH"] == "/usr/bin"

    def test_uvicorn_non_config_exit_reraises(self):
        """SystemExit(3) with no config-error flag re-raises untouched."""
        settings = make_settings()
        server = MagicMock()
        server.run = MagicMock(side_effect=SystemExit(3))
        from typer.testing import CliRunner

        with (
            patch.object(main, "resolve_config_path", return_value=None),
            patch.object(main, "KlangkSettings", return_value=settings),
            patch.object(main, "check_pid_preflight", return_value=None),
            patch.object(main, "_check_port_collisions"),
            patch.object(main, "build_app") as build,
            patch.object(
                main,
                "make_graceful_exit_server",
                return_value=lambda cfg: server,
            ),
            patch.object(main, "config_error_exit_status", return_value=None),
            patch.object(main, "prepend_gnubin_paths"),
        ):
            built = MagicMock()
            built.state.util.set_uds_mode = MagicMock()
            build.return_value = built
            result = CliRunner().invoke(main.app, [])
        assert result.exit_code == 3

    def test_report_pid_collision_without_marker_always_logs(self, tmp_path):
        """No instance-id on disk -> marker None -> log every time."""
        settings = make_settings({"KLANGKD_STATE_DIR": str(tmp_path)})
        assert main.refusal_marker_path(settings) is None
        main._report_pid_collision(settings, 111)  # logs, skips marking
        main._report_pid_collision(settings, 222)  # logs again (no dedup)

    def test_check_port_collisions_skips_headless_ports(self):
        settings = types.SimpleNamespace(
            port=None,  # headless: no browser proxy port to probe
            egress_port="5998",
            listen="127.0.0.1",
            egress_listen="127.0.0.1",
        )
        with patch.object(main, "check_port_preflight", return_value=False):
            main._check_port_collisions(settings)  # browser arm skipped
