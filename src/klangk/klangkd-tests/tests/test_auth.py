"""Tests for auth module: password hashing, JWT tokens, login/register."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from klangk import auth
from klangk.exceptions import ConfigurationError
from sqlalchemy.exc import IntegrityError as SAIntegrityError
import types as _types

from _helpers import make_settings
from klangk.auth import Auth, password_class_counts


def _auth(env=None):
    """Build an Auth instance from explicit env (no os.environ)."""
    from _helpers import wire_db_and_model

    state = _types.SimpleNamespace(
        state=_types.SimpleNamespace(settings=make_settings(env))
    )
    wire_db_and_model(state)
    return Auth(state)


def _req(auth=None):
    """A request-like whose ``app.state`` is the auth's app_state.

    Exposes ``app.state.auth`` (the FastAPI dep reads it) plus the
    ``model``/``db`` the dep callables reach (#1572).
    """
    if auth is None:
        auth = _auth()
    auth.app.state.auth = auth
    return _types.SimpleNamespace(app=auth.app)


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = auth.hash_password("mypassword")
        assert auth.verify_password("mypassword", hashed)

    def test_wrong_password_fails(self):
        hashed = auth.hash_password("mypassword")
        assert not auth.verify_password("wrongpassword", hashed)

    def test_different_hashes_for_same_password(self):
        h1 = auth.hash_password("same")
        h2 = auth.hash_password("same")
        assert h1 != h2  # PBKDF2 uses a random salt

    def test_hash_format_is_self_describing(self):
        # pbkdf2_sha512$<iterations>$<b64-salt>$<b64-digest> (#2576)
        hashed = auth.hash_password("mypassword")
        scheme, iterations, b64_salt, b64_digest = hashed.split("$")
        assert scheme == "pbkdf2_sha512"
        assert int(iterations) == auth.PBKDF2_ITERATIONS
        # 16-byte salt, 64-byte SHA-512 digest, standard base64
        import base64

        assert len(base64.b64decode(b64_salt)) == 16
        assert len(base64.b64decode(b64_digest)) == 64

    def test_verify_uses_stored_iteration_count(self):
        # conftest drops PBKDF2_ITERATIONS for suite speed; a hash made
        # at a different count must still verify (self-describing format).
        hashed = auth.hash_password("mypassword")
        scheme, iterations, _, _ = hashed.split("$")
        assert int(iterations) == 1_000  # the patched value, embedded
        assert auth.verify_password("mypassword", hashed)

    def test_production_strength_round_trip(self, monkeypatch):
        monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 600_000)
        hashed = auth.hash_password("mypassword")
        assert hashed.split("$")[1] == "600000"
        assert auth.verify_password("mypassword", hashed)
        assert not auth.verify_password("wrongpassword", hashed)

    def test_verify_rejects_malformed_hashes(self):
        for bad in (
            "",  # empty
            "not-a-hash",  # too few fields
            "pbkdf2_sha512$1000$short$extra$field",  # too many fields
            "bcrypt$12$deadbeef$deadbeef",  # unknown scheme
            "pbkdf2_sha512$abc$c2FsdA==$ZGF0YQ==",  # non-integer count
            "pbkdf2_sha512$0$c2FsdA==$ZGF0YQ==",  # zero iterations
            "pbkdf2_sha512$-1$c2FsdA==$ZGF0YQ==",  # negative count
            "pbkdf2_sha512$99999999999$c2FsdA==$ZGF0YQ==",  # over ceiling
            "pbkdf2_sha512$1000$!!!$ZGF0YQ==",  # invalid salt base64
            "pbkdf2_sha512$1000$c2FsdA==$!!!",  # invalid digest base64
            "$2b$12$KIXQeQwGjGIdLxL7ZoOeleTFjU3sKvzC"  # legacy bcrypt shape
            "aJm2WQ0GWZ8qZah9v1O",
        ):
            assert not auth.verify_password("mypassword", bad), bad

    def test_bcrypt_dependency_is_gone(self):
        # #2576: bcrypt must not be importable anywhere in the package's
        # environment (no direct use, no transitive dependency).
        import importlib.util

        assert importlib.util.find_spec("bcrypt") is None

    def test_hash_password_rejects_over_72_bytes(self):
        long_pw = "a" * 73
        with pytest.raises(ValueError, match="exceeds 72 bytes"):
            auth.hash_password(long_pw)

    def test_verify_password_over_72_bytes_returns_false(self):
        hashed = auth.hash_password("shortpassword")
        assert not auth.verify_password("a" * 73, hashed)

    def test_hash_password_accepts_exactly_72_bytes(self):
        pw = "a" * 72
        hashed = auth.hash_password(pw)
        assert auth.verify_password(pw, hashed)


class TestSecurityDefaults:
    """Lock in the hardened auth defaults introduced in #938.

    Auth reads settings at construction (#1501), so the production
    defaults are asserted directly — no subprocess dance needed (that
    was a workaround for the import-time globals). A regression that
    weakens either default fails here.
    """

    def test_hardened_defaults_when_env_unset(self):
        a = _auth()  # unset policy env -> production defaults
        # min_password_length=8, login_lockout_failures=5 (both on by
        # default — brute-force protection and a sane password floor).
        assert a.min_password_length == 8
        assert a.login_lockout_failures == 5


class TestValidatePasswordLength:
    def test_rejects_short_password(self):
        with pytest.raises(HTTPException) as exc_info:
            _auth().validate_password_length("")
        assert exc_info.value.status_code == 400
        assert "at least" in exc_info.value.detail

    def test_rejects_over_72_bytes(self):
        with pytest.raises(HTTPException) as exc_info:
            _auth().validate_password_length("a" * 73)
        assert exc_info.value.status_code == 400
        assert "72 bytes" in exc_info.value.detail

    def test_rejects_multibyte_over_72_bytes(self):
        pw = "\u00e9" * 37  # 2 bytes each = 74 bytes
        with pytest.raises(HTTPException) as exc_info:
            _auth().validate_password_length(pw)
        assert exc_info.value.status_code == 400

    def test_accepts_valid_password(self):
        _auth().validate_password_length("goodpass")


class TestValidatePasswordComplexity:
    """Character-class counts (#2581). Defaults are all 0 (off)."""

    def test_defaults_accept_anything(self):
        a = _auth()
        assert a.password_requirements == {
            "upper": 0,
            "lower": 0,
            "digit": 0,
            "special": 0,
        }
        a.validate_password_complexity("alllowercase")

    def test_requirements_read_from_settings(self):
        a = _auth(
            {
                "KLANGKD_PASSWORD_REQUIRE_UPPER": "1",
                "KLANGKD_PASSWORD_REQUIRE_LOWER": "2",
                "KLANGKD_PASSWORD_REQUIRE_DIGIT": "3",
                "KLANGKD_PASSWORD_REQUIRE_SPECIAL": "4",
            }
        )
        assert a.password_requirements == {
            "upper": 1,
            "lower": 2,
            "digit": 3,
            "special": 4,
        }

    def test_each_class_enforced(self):
        good = "Aa1!"
        cases = {
            "KLANGKD_PASSWORD_REQUIRE_UPPER": "zaa1!",
            "KLANGKD_PASSWORD_REQUIRE_LOWER": "ZAA1!",
            "KLANGKD_PASSWORD_REQUIRE_DIGIT": "Zaaa!",
            "KLANGKD_PASSWORD_REQUIRE_SPECIAL": "Zaa11",
        }
        for env, bad in cases.items():
            a = _auth({env: "1"})
            a.validate_password_complexity(good)
            with pytest.raises(HTTPException) as exc_info:
                a.validate_password_complexity(bad)
            assert exc_info.value.status_code == 400

    def test_count_greater_than_one(self):
        a = _auth({"KLANGKD_PASSWORD_REQUIRE_UPPER": "2"})
        a.validate_password_complexity("ABcdefg1")
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_complexity("Abcdefg1")
        # 2 uppercase letters -> plural form in the message
        assert "at least 2 uppercase letters" in exc_info.value.detail

    def test_lists_every_unmet_requirement(self):
        a = _auth(
            {
                "KLANGKD_PASSWORD_REQUIRE_UPPER": "1",
                "KLANGKD_PASSWORD_REQUIRE_DIGIT": "1",
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_complexity("plain")
        detail = exc_info.value.detail
        assert "1 uppercase letter" in detail
        assert "1 digit" in detail

    def test_special_counts_non_alnum(self):
        a = _auth({"KLANGKD_PASSWORD_REQUIRE_SPECIAL": "1"})
        a.validate_password_complexity("Password1!")
        with pytest.raises(HTTPException):
            a.validate_password_complexity("Password1")

    def test_classes_are_ascii(self):
        """Unicode letters/digits are special, not letters/digits (#2581).

        Parity with the Flutter ``PasswordPolicy`` and the CLI mirror:
        ``é`` is not a lowercase letter, ``²``/``٣`` are not digits,
        ``Ⅰ`` is not uppercase — they all count as special characters.
        """
        # é is special, satisfies REQUIRE_SPECIAL but not REQUIRE_LOWER.
        a = _auth(
            {
                "KLANGKD_PASSWORD_REQUIRE_LOWER": "1",
                "KLANGKD_PASSWORD_REQUIRE_SPECIAL": "1",
            }
        )
        a.validate_password_complexity("Aé1!aaaa")
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_complexity("A1!éééé")
        assert "1 lowercase letter" in exc_info.value.detail

        # ² and ٣ are not digits.
        a = _auth({"KLANGKD_PASSWORD_REQUIRE_DIGIT": "1"})
        with pytest.raises(HTTPException):
            a.validate_password_complexity("ab²٣cd!")

        # Ⅰ (Roman numeral) is not uppercase.
        a = _auth({"KLANGKD_PASSWORD_REQUIRE_UPPER": "1"})
        with pytest.raises(HTTPException):
            a.validate_password_complexity("aⅠb1!cde")

    def test_password_class_counts_helper(self):
        """The shared counter used by validation and startup seeding."""
        assert password_class_counts("") == {
            "upper": 0,
            "lower": 0,
            "digit": 0,
            "special": 0,
        }
        assert password_class_counts("Aa1!é²Ⅰ😀") == {
            "upper": 1,
            "lower": 1,
            "digit": 1,
            "special": 5,
        }

    def test_validate_password_runs_both_checks(self):
        a = _auth({"KLANGKD_PASSWORD_REQUIRE_DIGIT": "1"})
        # Long enough but no digit -> complexity rejects via the wrapper.
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password("nodigitshere")
        assert "digit" in exc_info.value.detail
        a.validate_password("has1digit")


class TestJWT:
    def test_create_and_decode_token(self):
        token = _auth().create_token("user-123", "alice@example.com")
        payload = _auth().decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["email"] == "alice@example.com"
        assert "jti" in payload
        assert "exp" in payload

    def test_invalid_token_raises(self):
        from jose import JWTError

        with pytest.raises(JWTError):
            _auth().decode_token("garbage.token.value")


class TestRegister:
    async def test_register_disabled(self, db):
        a = _auth({"KLANGKD_DISABLE_REGISTRATION": "true"})
        with pytest.raises(HTTPException) as exc_info:
            await a.register(
                auth.RegisterRequest(
                    email="blocked@example.com", password="pass1234"
                )
            )
        assert exc_info.value.status_code == 403

    async def test_register_success(self, db):
        result = await _auth().register(
            auth.RegisterRequest(email="new@example.com", password="pass1234")
        )
        assert result.user_id
        assert result.email == "new@example.com"
        assert result.access_token is None  # unverified, no token

    async def test_register_verified(self, db):
        a = _auth()
        result = await a.register(
            auth.RegisterRequest(
                email="verified@example.com", password="pass1234"
            ),
            verified=True,
        )
        assert result.access_token
        assert result.email == "verified@example.com"
        # Auto-verify mints a session: that first session is a login
        # and stamps last_login_at (#2583).
        row = await a.app.state.model.users.get_user_by_id(result.user_id)
        assert row["last_login_at"] is not None

    async def test_register_duplicate_email(self, db):
        await _auth().register(
            auth.RegisterRequest(email="dup@example.com", password="pass1234")
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().register(
                auth.RegisterRequest(
                    email="dup@example.com", password="pass5678"
                )
            )
        assert exc_info.value.status_code == 400

    async def test_register_race_integrity_error(self, db, app_state):
        """If a concurrent registration wins the UNIQUE constraint,
        the loser must get a clean 400 rather than an unhandled 500
        (regression for #877)."""
        a = _auth()
        with patch.object(
            a.app.state.model.users,
            "create_user",
            side_effect=SAIntegrityError(
                "statement", {}, Exception("UNIQUE constraint failed")
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await a.register(
                    auth.RegisterRequest(
                        email="race@example.com", password="pass1234"
                    )
                )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Registration failed"

    async def test_register_invalid_email(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().register(
                auth.RegisterRequest(email="not-an-email", password="pass1234")
            )
        assert exc_info.value.status_code == 400

    async def test_register_short_password(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().register(
                auth.RegisterRequest(email="valid@example.com", password="abc")
            )
        assert exc_info.value.status_code == 400

    async def test_register_password_length_configurable(self, db):
        """min_password_length is read from settings at construction."""
        # A non-default floor (10): 9 chars fails, 10 succeeds.
        a = _auth({"KLANGKD_MIN_PASSWORD_LENGTH": "10"})
        with pytest.raises(HTTPException) as exc_info:
            await a.register(
                auth.RegisterRequest(
                    email="valid@example.com", password="123456789"
                )
            )
        assert exc_info.value.status_code == 400
        assert (
            "10" in exc_info.value.detail
        )  # error message includes the length

        # 10 chars should succeed
        result = await a.register(
            auth.RegisterRequest(
                email="valid2@example.com", password="1234567890"
            )
        )
        assert result.user_id


class TestLogin:
    async def test_login_success(self, user):
        a = _auth()
        result = await a.login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token
        assert result.token_type == "bearer"
        # A successful login stamps last_login_at (#2583).
        row = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row["last_login_at"] is not None

    async def test_login_failure_does_not_stamp(self, user):
        """Only successful logins stamp last_login_at (#2583)."""
        a = _auth()
        with pytest.raises(HTTPException):
            await a.login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="wrong"
                )
            )
        row = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row["last_login_at"] is None

    async def test_login_success_by_handle(self, user):
        """Login accepts a handle as well as an email (#616)."""
        result = await _auth().login(
            auth.LoginRequest(identifier=user["handle"], password="testpass")
        )
        assert result.access_token

    async def test_login_wrong_password_by_handle(self, user):
        """A bad password presented with a handle still 401s (#616)."""
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(identifier=user["handle"], password="wrong")
            )
        assert exc_info.value.status_code == 401

    async def test_login_wrong_password(self, user):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 401

    async def test_login_oidc_only_user_no_password_hash(self, db, app_state):
        """OIDC-only users have no password hash; login must return 401
        (Invalid credentials) rather than crashing with a 500."""
        await app_state.state.model.users.create_user(
            "oidc@example.com", None, verified=True, provider="oidc"
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="oidc@example.com", password="anything"
                )
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid credentials"

    async def test_login_unverified(self, db, app_state):
        password_hash = auth.hash_password("testpass")
        await app_state.state.model.users.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="unverified@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 403
        assert "not verified" in exc_info.value.detail

    async def test_login_nonexistent_user(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="noone@example.com", password="pass"
                )
            )
        assert exc_info.value.status_code == 401


class TestLoginRateLimit:
    """Tests for login brute-force protection.

    The default LOGIN_LOCKOUT_FAILURES is 5 (enabled); ``_auth()`` builds
    ``Auth(make_settings({}))`` which picks up that default, so these tests
    exercise the lockout machinery deterministically (#1515: auth reads
    from settings at construction, not module globals — the old reload
    dance is obsolete).
    """

    async def test_login_wrong_password_records_attempt(self, user, app_state):
        """Wrong password increments attempt count."""
        for i in range(_auth().login_lockout_failures - 1):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(
                        identifier="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "testuser@example.com"
            )
        )
        assert info["attempt_count"] == _auth().login_lockout_failures - 1

    async def test_login_handle_attempts_rekeyed_to_email(
        self, user, app_state
    ):
        """Failed attempts presented by *handle* are recorded against the
        resolved user's canonical email, so handle and email attempts
        share one lockout counter (#616)."""
        handle = user["handle"]
        for i in range(_auth().login_lockout_failures - 1):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(identifier=handle, password="wrong")
                )
            assert exc_info.value.status_code == 401
        # counter lives under the canonical email, not the raw handle
        by_email = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "testuser@example.com"
            )
        )
        assert by_email["attempt_count"] == _auth().login_lockout_failures - 1
        assert (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                handle
            )
            is None
        )

    async def test_login_lockout_after_max_attempts(self, user):
        """Locked out after LOGIN_LOCKOUT_FAILURES failed attempts."""
        for i in range(_auth().login_lockout_failures):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(
                        identifier="testuser@example.com", password="wrong"
                    )
                )
            if i < _auth().login_lockout_failures - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 429

    async def test_login_resets_count_after_window(self, user, app_state):
        """Failures older than the window don't accumulate to a lockout.

        Seed a near-threshold count with an old first_attempt_at; the
        next failed login should reset (not lock), so a user can't be
        permanently locked out by failures spread across days.
        """
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "INSERT INTO login_attempts"
                " (email, attempt_count, first_attempt_at)"
                " VALUES (?, ?, ?)",
                (
                    "testuser@example.com",
                    _auth().login_lockout_failures - 1,
                    old,
                ),
            )
        # A wrong password now: window elapsed -> reset, not lock.
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 401  # not 429
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "testuser@example.com"
            )
        )
        assert info["attempt_count"] == 1

    def test_window_elapsed_policy(self):
        """window_elapsed decides whether the sliding window has passed."""
        assert _auth().window_elapsed(None) is False
        assert _auth().window_elapsed({"first_attempt_at": None}) is False
        # Unparseable timestamp is treated as not-elapsed (safe default).
        assert (
            _auth().window_elapsed({"first_attempt_at": "not-a-date"}) is False
        )
        old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=_auth().login_lockout_window + 1)
        ).isoformat()
        assert _auth().window_elapsed({"first_attempt_at": old}) is True
        recent = datetime.now(timezone.utc).isoformat()
        assert _auth().window_elapsed({"first_attempt_at": recent}) is False

    async def test_login_lockout_message_shows_remaining_time(
        self, user, app_state
    ):
        """Lockout message includes remaining minutes."""
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        await app_state.state.model.login_attempts.record_failed_login(
            "testuser@example.com"
        )
        await app_state.state.model.login_attempts.set_login_lockout(
            "testuser@example.com", locked_until.isoformat()
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 429
        assert "minutes" in exc_info.value.detail

    async def test_expired_lockout_allows_login(self, user, app_state):
        """An expired lockout doesn't block the user from logging in."""
        expired_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await app_state.state.model.login_attempts.record_failed_login(
            "testuser@example.com"
        )
        await app_state.state.model.login_attempts.set_login_lockout(
            "testuser@example.com", expired_until.isoformat()
        )
        result = await _auth().login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token

    async def test_login_blocked_while_lockout_active(self, user, app_state):
        """Active lockout returns 429 with a countdown."""
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        await app_state.state.model.login_attempts.record_failed_login(
            "testuser@example.com"
        )
        await app_state.state.model.login_attempts.set_login_lockout(
            "testuser@example.com", locked_until.isoformat()
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 429

    async def test_should_lockout_helper(self, db):
        """should_lockout returns True at threshold, False below/above."""
        assert _auth().should_lockout({"attempt_count": 4}) is False
        assert _auth().should_lockout({"attempt_count": 5}) is True
        assert _auth().should_lockout(None) is False

    async def test_should_lockout_respects_configured_threshold(self, db):
        """should_lockout uses LOGIN_LOCKOUT_FAILURES as the threshold."""
        assert _auth().should_lockout({"attempt_count": 5}) is True
        assert _auth().should_lockout({"attempt_count": 4}) is False

    async def test_is_locked_out_helper(self, db):
        """is_locked_out returns True/msg when locked_until is in the future."""
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        locked = {"attempt_count": 5, "locked_until": future.isoformat()}
        is_locked, msg = auth.is_locked_out(locked)
        assert is_locked is True
        assert "minutes" in msg
        expired = {"attempt_count": 5, "locked_until": past.isoformat()}
        is_locked2, msg2 = auth.is_locked_out(expired)
        assert is_locked2 is False
        assert msg2 is None
        no_lock = {"attempt_count": 1, "locked_until": None}
        assert auth.is_locked_out(no_lock) == (False, None)
        assert auth.is_locked_out(None) == (False, None)

    async def test_login_lockout_disabled_when_zero(self, db, user):
        """With login_lockout_failures=0, no rate limiting occurs."""
        a = _auth({"KLANGKD_LOGIN_LOCKOUT_FAILURES": "0"})
        for _ in range(20):
            with pytest.raises(HTTPException) as exc_info:
                await a.login(
                    auth.LoginRequest(
                        identifier="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401
        # The success path's counter clear is a guarded no-op, not a
        # crash, when lockout is disabled.
        result = await a.login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token

    async def test_login_nonexistent_user_also_rate_limited(self, db):
        """Nonexistent users are also rate-limited to prevent enumeration."""
        for i in range(_auth().login_lockout_failures):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(
                        identifier="nobody@example.com", password="wrong"
                    )
                )
            if i < _auth().login_lockout_failures - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429

    async def test_unknown_user_still_burns_a_verify(self, db, monkeypatch):
        """The unknown-identifier path runs one full verify against the
        dummy hash, so its response timing matches the wrong-password
        path and accounts can't be enumerated by timing (#2618)."""
        calls: list[str] = []
        real = auth.verify_password

        def counting(password, hashed):
            calls.append(hashed)
            return real(password, hashed)

        monkeypatch.setattr(auth, "verify_password", counting)
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    identifier="noone@example.com", password="guess"
                )
            )
        assert exc_info.value.status_code == 401
        assert calls == [auth.dummy_verify_hash()]

    async def test_login_clears_attempts(self, db, user, app_state):
        """Successful login clears failed attempt counts."""
        await app_state.state.model.login_attempts.record_failed_login(
            "testuser@example.com"
        )
        await app_state.state.model.login_attempts.record_failed_login(
            "testuser@example.com"
        )
        result = await _auth().login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "testuser@example.com"
            )
        )
        assert info is None


class TestVerification:
    def test_create_and_decode_verification_token(self):
        token = _auth().create_verification_token("user-123")
        user_id = _auth().decode_verification_token(token)
        assert user_id == "user-123"

    def test_decode_invalid_token(self):
        assert _auth().decode_verification_token("garbage") is None

    def test_decode_wrong_purpose(self):
        # A regular auth token should not pass as a verification token
        token = _auth().create_token("user-123", "test")
        assert _auth().decode_verification_token(token) is None

    async def test_verify_user(self, db, app_state):
        password_hash = auth.hash_password("pass")
        user = await app_state.state.model.users.create_user(
            "toverify@example.com", password_hash, verified=False
        )
        assert not user["verified"]
        result = await app_state.state.model.users.verify_user(user["id"])
        assert result is True
        updated = await app_state.state.model.users.get_user_by_email(
            "toverify@example.com"
        )
        assert updated["verified"] is True

    async def test_verify_nonexistent_user(self, db, app_state):
        result = await app_state.state.model.users.verify_user(
            "nonexistent-id"
        )
        assert result is False


class TestPasswordReset:
    def test_create_and_decode_reset_token(self):
        token = _auth().create_password_reset_token("user-456")
        assert _auth().decode_password_reset_token(token) == "user-456"

    def test_decode_invalid_token(self):
        assert _auth().decode_password_reset_token("garbage") is None

    def test_reset_and_verify_tokens_not_interchangeable(self):
        reset = _auth().create_password_reset_token("user-456")
        verify = _auth().create_verification_token("user-456")
        assert _auth().decode_verification_token(reset) is None
        assert _auth().decode_password_reset_token(verify) is None


class TestWorkspaceToken:
    def test_create_and_decode_workspace_token(self):
        token = _auth().create_workspace_token("ws-123")
        assert _auth().decode_workspace_token(token) == "ws-123"

    def test_decode_invalid_token(self):
        assert _auth().decode_workspace_token("garbage") is None

    def test_user_token_rejected(self):
        user_token = _auth().create_token("user-1", "u@test.com")
        assert _auth().decode_workspace_token(user_token) is None

    def test_verify_token_rejected(self):
        verify_token = _auth().create_verification_token("user-1")
        assert _auth().decode_workspace_token(verify_token) is None

    def test_workspace_token_rejected_by_other_decoders(self):
        ws_token = _auth().create_workspace_token("ws-123")
        assert _auth().decode_verification_token(ws_token) is None
        assert _auth().decode_password_reset_token(ws_token) is None


class TestTokenValidation:
    async def test_get_user_from_valid_token(self, user):
        token = _auth().create_token(user["id"], user["email"])
        result = await _auth().get_user_from_token(token)
        assert result is not None
        assert result["id"] == user["id"]

    async def test_expired_password_rejected_on_ws(
        self, app_state, db, monkeypatch
    ):
        """An expired password rejects WS auth like a dead token (#3177),
        mirroring the disabled-account treatment (#2588)."""
        monkeypatch.setattr(
            app_state.state.settings,
            "password_max_age_days",
            60,
            raising=False,
        )
        a = Auth(app_state)
        pw_hash = auth.hash_password("testpass")
        user = await app_state.state.model.users.create_user(
            "ws-exp@example.com", pw_hash, verified=True
        )
        token = a.create_token(user["id"], user["email"])
        assert await a.get_user_from_token(token) is not None
        old = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ?, created_at = ?"
                " WHERE id = ?",
                (old, old, user["id"]),
            )
        assert await a.get_user_from_token(token) is None

    async def test_get_user_from_invalid_token(self, db):
        result = await _auth().get_user_from_token("invalid.token.here")
        assert result is None

    async def test_blocklisted_token_rejected(self, user):
        token = _auth().create_token(user["id"], user["email"])
        # Token should work before blocklisting
        assert await _auth().get_user_from_token(token) is not None
        # Blocklist it
        await _auth().logout(token)
        # Now it should fail
        assert await _auth().get_user_from_token(token) is None

    async def test_get_user_from_token_missing_sub(self, db):
        """Token with no 'sub' claim returns None."""
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        assert await _auth().get_user_from_token(token) is None

    async def test_get_user_from_token_missing_jti(self, db):
        """Token with no 'jti' claim returns None."""
        token = jwt.encode(
            {"sub": "uid", "email": "x", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        assert await _auth().get_user_from_token(token) is None

    async def test_get_user_from_token_deleted_user(self, user):
        """Token for a user that no longer exists returns None."""
        token = _auth().create_token("nonexistent-id", "ghost@example.com")
        assert await _auth().get_user_from_token(token) is None

    async def test_get_user_from_token_expired(self, db):
        """A valid-signature but expired token returns TOKEN_EXPIRED."""
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        token = jwt.encode(
            {
                "sub": "uid",
                "email": "x",
                "jti": "j1",
                "exp": expired,
            },
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        result = await _auth().get_user_from_token(token)
        assert result is _auth().TOKEN_EXPIRED


class TestGetCurrentUser:
    async def test_valid_credentials(self, user):
        token = _auth().create_token(user["id"], user["email"])
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user(_req(), creds)
        assert result["id"] == user["id"]

    async def test_no_credentials(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_req(), None)
        assert exc_info.value.status_code == 401

    async def test_invalid_token(self, db):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="bad.token.here"
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_req(), creds)
        assert exc_info.value.status_code == 401

    async def test_missing_sub_in_token(self, db):
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_req(), creds)
        assert exc_info.value.status_code == 401

    async def test_blocklisted_token(self, user):
        token = _auth().create_token(user["id"], user["email"])
        await _auth().logout(token)
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_req(), creds)
        assert exc_info.value.status_code == 401

    async def test_deleted_user(self, user):
        token = _auth().create_token("nonexistent-id", "ghost@example.com")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(_req(), creds)
        assert exc_info.value.status_code == 401


class TestGetCurrentUserOptional:
    async def test_valid_credentials(self, user):
        token = _auth().create_token(user["id"], user["email"])
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(_req(), creds)
        assert result is not None
        assert result["id"] == user["id"]

    async def test_no_credentials(self, db):
        result = await auth.get_current_user_optional(_req(), None)
        assert result is None

    async def test_invalid_token(self, db):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="bad.token"
        )
        result = await auth.get_current_user_optional(_req(), creds)
        assert result is None

    async def test_missing_sub(self, db):
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(_req(), creds)
        assert result is None

    async def test_blocklisted_token(self, user):
        token = _auth().create_token(user["id"], user["email"])
        await _auth().logout(token)
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(_req(), creds)
        assert result is None

    async def test_deleted_user(self, user):
        token = _auth().create_token("nonexistent-id", "ghost@example.com")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(_req(), creds)
        assert result is None


class TestLogout:
    async def test_logout_invalid_token(self, db):
        """Logout with garbage token should not raise."""
        await _auth().logout("not.a.valid.token")

    async def test_logout_token_without_jti(self, db):
        """Logout with token missing jti should not raise."""
        token = jwt.encode(
            {"sub": "uid", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        await _auth().logout(token)


class TestRefreshToken:
    async def test_refresh_returns_new_token(self, db, app_state):
        """Refreshing a valid token returns a new token."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        token = _auth().create_token(user["id"], user["email"])
        result = await _auth().refresh_token(token)
        assert result.access_token != token
        # Old JTI should be blocklisted
        old_payload = _auth().decode_token(token, allow_expired=True)
        assert await app_state.state.model.tokens.is_token_blocklisted(
            old_payload["jti"]
        )

    async def test_refresh_idempotent(self, db, app_state):
        """Refreshing the same token twice returns the same new token."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        token = _auth().create_token(user["id"], user["email"])
        result1 = await _auth().refresh_token(token)
        result2 = await _auth().refresh_token(token)
        assert result1.access_token == result2.access_token

    async def test_refresh_expired_token_returns_401(self, db, app_state):
        """Refreshing an expired token with no prior refresh returns 401."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "expired-jti",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().refresh_token(expired)
        assert exc_info.value.status_code == 401

    async def test_refresh_expired_token_with_prior_refresh(
        self, db, app_state
    ):
        """Refreshing an expired token returns cached new token if within window."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        # Simulate a token that was refreshed, then expired
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        await app_state.state.model.tokens.blocklist_token(
            "old-jti", expires_at, new_token="cached-new-token"
        )
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "old-jti",
                "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
            },
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        result = await _auth().refresh_token(expired)
        assert result.access_token == "cached-new-token"

    async def test_refresh_expired_returns_cached_regardless_of_blocklist_expiry(
        self, db, app_state
    ):
        """Cached replacement is returned even when the old token's
        blocklist expires_at has passed — the new token's own exp
        governs its validity."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        expires_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        await app_state.state.model.tokens.blocklist_token(
            "old-jti", expires_at, new_token="cached-replacement"
        )
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "old-jti",
                "exp": datetime.now(timezone.utc) - timedelta(hours=2),
            },
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        result = await _auth().refresh_token(expired)
        assert result.access_token == "cached-replacement"

    async def test_refresh_deleted_user_returns_401(self, db):
        """Refreshing a token for a deleted user returns 401."""
        token = _auth().create_token("nonexistent-user", "gone@example.com")
        with pytest.raises(HTTPException) as exc_info:
            await _auth().refresh_token(token)
        assert exc_info.value.status_code == 401

    async def test_refresh_revoked_token_returns_401(self, db, app_state):
        """Refreshing a revoked (logged out) token returns 401."""
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        token = _auth().create_token(user["id"], user["email"])
        await _auth().logout(token)
        with pytest.raises(HTTPException) as exc_info:
            await _auth().refresh_token(token)
        assert exc_info.value.status_code == 401

    def test_configurable_token_expire_hours(self):
        """token_expire_hours reads from settings at construction."""
        a = _auth({"KLANGKD_ACCESS_TOKEN_HOURS": "48"})
        assert a.token_expire_hours == 48.0


class TestInvitationTokens:
    def test_roundtrip(self):
        token = _auth().create_invitation_token("inv-123", "user@example.com")
        result = _auth().decode_invitation_token(token)
        assert result == ("inv-123", "user@example.com")

    def test_wrong_purpose_rejected(self):
        token = _auth().create_verification_token("uid")
        assert _auth().decode_invitation_token(token) is None

    def test_invalid_token_returns_none(self):
        assert _auth().decode_invitation_token("garbage") is None

    def test_missing_email_returns_none(self):
        token = jwt.encode(
            {"sub": "inv-1", "purpose": "invite", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        assert _auth().decode_invitation_token(token) is None

    def test_missing_sub_returns_none(self):
        token = jwt.encode(
            {"email": "x@y.com", "purpose": "invite", "exp": 9999999999},
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        assert _auth().decode_invitation_token(token) is None


class TestInvitationsEnabled:
    def test_enabled_by_default(self):
        assert _auth().invitations_enabled() is True

    def test_disabled(self):
        a = _auth({"KLANGKD_DISABLE_INVITES": "true"})
        assert a.invitations_enabled() is False


class TestRequireSecureJwtSecret:
    def test_secure_secret_passes(self):
        a = _auth({"KLANGKD_JWT_SECRET": "a-real-strong-secret"})
        assert a.jwt_secret_is_secure() is True
        a.require_secure_jwt_secret()  # no raise

    def test_default_secret_is_insecure(self):
        a = _auth()  # unset -> INSECURE_DEFAULT_SECRET
        assert a.jwt_secret_is_secure() is False

    def test_default_secret_warns(self, caplog):
        a = _auth()
        import logging

        with caplog.at_level(logging.WARNING, logger="klangk.auth"):
            a.require_secure_jwt_secret()  # warns but does not raise

    def test_default_secret_blocks_with_prevent(self):
        a = _auth({"KLANGKD_PREVENT_INSECURE_JWT_SECRET": "1"})
        with pytest.raises(ConfigurationError, match="KLANGKD_JWT_SECRET"):
            a.require_secure_jwt_secret()

    def test_empty_secret_blocks_with_prevent(self):
        # jwt_secret unset falls back to the insecure default; an explicit
        # empty string is also insecure and blocked when prevent is set.
        a = _auth(
            {
                "KLANGKD_JWT_SECRET": "",
                "KLANGKD_PREVENT_INSECURE_JWT_SECRET": "1",
            }
        )
        assert a.jwt_secret_is_secure() is False
        with pytest.raises(ConfigurationError):
            a.require_secure_jwt_secret()


class TestValidatePasswordNotReused:
    """Current + retired-hash reuse checks (#2582).

    Semantics: history holds hashes the user changed *away* from; the
    current hash is checked separately from ``users``.
    """

    def _auth_with_history(self, app_state, monkeypatch, count=3):
        monkeypatch.setattr(
            app_state.state.settings,
            "password_history_count",
            count,
            raising=False,
        )
        return Auth(app_state)

    async def _user_at_password(self, app_state, *hashes):
        """Create a user whose current hash is the last of *hashes*.

        Each extra hash is one change away (retiring the previous one).
        """
        user = await app_state.state.model.users.create_user(
            "reuse@example.com", hashes[0], verified=True
        )
        for h in hashes[1:]:
            await app_state.state.model.users.update_password(user["id"], h)
        return user

    async def test_disabled_when_count_zero(self, app_state, db):
        a = Auth(app_state)  # default 0 -> no-op
        assert a.password_history_count == 0
        # Must not touch the DB / raise even for a matching password.
        await a.validate_password_not_reused("u", "anything")

    async def test_rejects_current_password(self, app_state, db, monkeypatch):
        a = self._auth_with_history(app_state, monkeypatch)
        real = auth.hash_password("currentpass")
        user = await self._user_at_password(app_state, "stale", real)
        with pytest.raises(HTTPException) as exc_info:
            await a.validate_password_not_reused(user["id"], "currentpass")
        assert exc_info.value.status_code == 400
        assert "current" in exc_info.value.detail

    async def test_rejects_retired_password(self, app_state, db, monkeypatch):
        a = self._auth_with_history(app_state, monkeypatch)
        old = auth.hash_password("oldpass")
        new = auth.hash_password("newpass")
        # The seed change retires `old` into history.
        user = await self._user_at_password(app_state, old, new)
        with pytest.raises(HTTPException) as exc_info:
            await a.validate_password_not_reused(user["id"], "oldpass")
        assert exc_info.value.status_code == 400
        assert "recently" in exc_info.value.detail

    async def test_accepts_novel_password(self, app_state, db, monkeypatch):
        a = self._auth_with_history(app_state, monkeypatch)
        old = auth.hash_password("oldpass")
        user = await self._user_at_password(app_state, old)
        await a.validate_password_not_reused(user["id"], "brand-new-pass")

    async def test_history_window_only(self, app_state, db, monkeypatch):
        """count=1 remembers exactly one previous password: the last one
        is rejected, the one before it is reusable again."""
        a = self._auth_with_history(app_state, monkeypatch, count=1)
        first = auth.hash_password("firstpass")
        second = auth.hash_password("secondpass")
        third = auth.hash_password("thirdpass")
        user = await self._user_at_password(app_state, first, second, third)
        # `second` was the most recent retirement -> rejected.
        with pytest.raises(HTTPException) as exc_info:
            await a.validate_password_not_reused(user["id"], "secondpass")
        assert "recently" in exc_info.value.detail
        # `first` was pruned out of the 1-slot window -> allowed.
        await a.validate_password_not_reused(user["id"], "firstpass")


class TestPasswordEditDistance:
    """Levenshtein distance over code points (#3173)."""

    def test_zero_for_identical(self):
        assert auth.password_edit_distance("same-pass", "same-pass") == 0

    def test_counts_substitutions(self):
        assert auth.password_edit_distance("Password1", "Password9") == 1

    def test_insertions_and_deletions_count(self):
        # The positional-diff workaround: prepending shifts every
        # position, yet the edit distance is a single insertion.
        assert auth.password_edit_distance("Password1", "xPassword1") == 1
        assert auth.password_edit_distance("Password1!", "Password1") == 1

    def test_mixed_edits(self):
        # 3 substitutions + 1 deletion ("test"->"new", drop the tail "1")
        assert auth.password_edit_distance("testpass", "newpass1") == 4

    def test_empty_sides(self):
        assert auth.password_edit_distance("", "abc") == 3
        assert auth.password_edit_distance("abc", "") == 3
        assert auth.password_edit_distance("", "") == 0

    def test_counts_code_points_not_bytes(self):
        # 4 emoji code points vs 1: distance 3 in code points, not the
        # UTF-16 code-unit or UTF-8 byte counts.
        assert auth.password_edit_distance("\U0001f600" * 4, "\U0001f600") == 3


class TestValidatePasswordChangedEnough:
    """The >= N changed-characters gate on self-service changes (#3173)."""

    def _auth_with(self, minimum):
        return _auth({"KLANGKD_PASSWORD_MIN_CHANGED": str(minimum)})

    def test_disabled_when_zero(self):
        a = self._auth_with(0)
        assert a.password_min_changed == 0
        # A one-character change (even no change) is fine when disarmed.
        a.validate_password_changed_enough("testpass", "testpass")

    def test_reads_setting_live(self):
        # Settings-derived values must not be snapshotted at construction
        # (#1608 pattern): a monkeypatched reload changes the gate.
        a = self._auth_with(0)
        a.app.state.settings.password_min_changed = 8
        assert a.password_min_changed == 8

    def test_rejects_too_similar(self):
        a = self._auth_with(8)
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_changed_enough("testpass", "testpas9")
        assert exc_info.value.status_code == 400
        assert "change at least 8 characters" in exc_info.value.detail

    def test_boundary_accepts_exactly_minimum(self):
        # distance("testpass", "Qwerty!234") == 8 — right at the line.
        self._auth_with(8).validate_password_changed_enough(
            "testpass", "Qwerty!234"
        )

    def test_rejects_one_below_minimum(self):
        a = self._auth_with(4)
        # distance("testpass", "newpass1") == 4 passes; 3 does not.
        a.validate_password_changed_enough("testpass", "newpass1")
        with pytest.raises(HTTPException):
            a.validate_password_changed_enough("testpass", "newpass")


class TestSessionLimit:
    """Concurrent-session limiting via KLANGKD_MAX_SESSIONS_PER_USER (#2585).

    Each login registers the issued JTI in user_sessions; past the cap the
    oldest session is revoked through the token blocklist (same path as
    logout: HTTP 401 "Token has been revoked", WS close 4001).
    """

    async def _login(self, env=None):
        """Log the fixture user in; return the issued token."""
        result = await _auth(env).login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        return result.access_token

    async def test_issue_token_records_session(self, user, app_state):
        token = await self._login()
        payload = _auth().decode_token(token)
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert [r["jti"] for r in rows] == [payload["jti"]]

    async def test_unlimited_by_default(self, user, app_state):
        tokens = [await self._login() for _ in range(4)]
        a = _auth()
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 4
        for token in tokens:
            assert await a.get_user_from_token(token) is not None

    async def test_limit_revokes_oldest_session(self, user, app_state):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "2"}
        first = await self._login(env)
        second = await self._login(env)
        third = await self._login(env)

        a = _auth(env)
        tokens_mod = app_state.state.model.tokens
        # The oldest (first) session is revoked via the blocklist...
        first_jti = a.decode_token(first)["jti"]
        assert await tokens_mod.is_token_blocklisted(first_jti)
        assert await a.get_user_from_token(first) is None
        # ...while the two newest sessions survive.
        assert await a.get_user_from_token(second) is not None
        assert await a.get_user_from_token(third) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 2

    async def test_limit_one_revokes_all_previous(self, user, app_state):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        first = await self._login(env)
        second = await self._login(env)
        a = _auth(env)
        assert await a.get_user_from_token(first) is None
        assert await a.get_user_from_token(second) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 1

    async def test_limit_of_two_keeps_two(self, user, app_state):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "2"}
        first = await self._login(env)
        second = await self._login(env)
        a = _auth(env)
        assert await a.get_user_from_token(first) is not None
        assert await a.get_user_from_token(second) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 2

    async def test_refresh_does_not_grow_session_count(self, user, app_state):
        """A refresh is the same session under a new token: it replaces the
        old JTI's row instead of adding one."""
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "2"}
        first = await self._login(env)
        second = await self._login(env)
        refreshed = await _auth(env).refresh_token(second)

        a = _auth(env)
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 2
        # The refreshed JTI now occupies the slot; the first login's
        # session is untouched by the refresh.
        assert await a.get_user_from_token(refreshed.access_token) is not None
        assert await a.get_user_from_token(first) is not None

    async def test_logout_frees_session_slot(self, user, app_state):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        first = await self._login(env)
        a = _auth(env)
        await a.logout(first)
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert rows == []
        # With the slot free, the next login evicts nothing.
        second = await self._login(env)
        assert await a.get_user_from_token(second) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 1

    async def test_expired_sessions_do_not_count_toward_limit(
        self, user, app_state
    ):
        """A row whose token already expired is purged before counting, so
        it neither occupies a slot nor gets blocklisted on eviction."""
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await app_state.state.model.sessions.record_session(
            user["id"], "jti-dead", past
        )
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        token = await self._login(env)
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert [r["jti"] for r in rows] == [_auth().decode_token(token)["jti"]]
        # The dead row was purged, not blocklisted.
        assert not (
            await app_state.state.model.tokens.is_token_blocklisted("jti-dead")
        )

    async def test_refresh_keeps_oldest_position(self, user, app_state):
        """Refreshing the oldest session does not make it the newest:
        eviction order is login time, so a later login still evicts the
        refreshed (oldest) session, not the younger idle one.
        """
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "2"}
        first = await self._login(env)
        second = await self._login(env)
        refreshed = await _auth(env).refresh_token(first)
        third = await self._login(env)
        a = _auth(env)
        assert await a.get_user_from_token(refreshed.access_token) is None
        assert await a.get_user_from_token(second) is not None
        assert await a.get_user_from_token(third) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 2

    async def test_refresh_of_untracked_token_enforces_limit(
        self, user, app_state
    ):
        """Refreshing a pre-#2585 token (no session row) inserts one; the
        cap is enforced on that path too, evicting the oldest session.
        """
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        a = _auth(env)
        tracked = await self._login(env)
        untracked = a.create_token(user["id"], user["email"])
        refreshed = await a.refresh_token(untracked)
        assert await a.get_user_from_token(tracked) is None
        assert await a.get_user_from_token(refreshed.access_token) is not None
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 1

    async def test_register_records_session(self, db, app_state):
        """The verified-registration path issues through issue_token too."""
        result = await _auth().register(
            auth.RegisterRequest(
                email="fresh@example.com", password="longenough1"
            ),
            verified=True,
        )
        assert result.access_token
        rows = await app_state.state.model.sessions.list_sessions(
            result.user_id
        )
        assert len(rows) == 1


class TestRevocationKicksSockets:
    """#3152: hard revocation (logout, session-limit eviction) closes the
    live WS connections the revoked token authenticated; refresh rotation
    (which blocklists the old JTI via _swap_token) must NOT kick — the
    session lives on under the new token."""

    def _auth_with_sockets(self, env=None):
        """An Auth whose app_state wires a real WebSocketState, plus that
        state — fake connections can be planted in ``sockets.connections``."""
        from klangk.wshandler.session import WebSocketState

        a = _auth(env)
        sockets = WebSocketState(a.app)
        a.app.state.sockets = sockets
        return a, sockets

    @staticmethod
    def _fake_conn(user, jti):
        return _types.SimpleNamespace(
            user={"id": user["id"], "email": user["email"]}, jti=jti
        )

    async def _login(self, a):
        result = await a.login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        return result.access_token

    async def test_logout_closes_socket_for_that_jti(self, user, app_state):
        a, sockets = self._auth_with_sockets()
        token = await self._login(a)
        jti = a.decode_token(token)["jti"]
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        sockets.connections[FakeSock()] = self._fake_conn(user, jti)
        sockets.connections[FakeSock()] = self._fake_conn(user, "other-jti")
        try:
            await a.logout(token)
            # Only the connection the revoked token authenticated is closed.
            assert closed == [(4001, "Token revoked")]
        finally:
            sockets.connections.clear()

    async def test_session_limit_eviction_closes_evicted_sockets(
        self, user, app_state
    ):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        a, sockets = self._auth_with_sockets(env)
        first = await self._login(a)
        first_jti = a.decode_token(first)["jti"]
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        sockets.connections[FakeSock()] = self._fake_conn(user, first_jti)
        try:
            # The second login evicts the first session past the cap of 1.
            second = await self._login(a)
            assert closed == [(4001, "Token revoked")]
            # The surviving session's token still works.
            assert await a.get_user_from_token(second) is not None
        finally:
            sockets.connections.clear()

    async def test_refresh_rotation_does_not_close_socket(
        self, user, app_state
    ):
        """A refresh blocklists the old JTI (idempotent rotation cache)
        but keeps the session — the socket opened with the old token must
        stay connected, retargeted onto the new JTI."""
        a, sockets = self._auth_with_sockets()
        token = await self._login(a)
        jti = a.decode_token(token)["jti"]
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        conn = self._fake_conn(user, jti)
        bystander = self._fake_conn(user, "unrelated-jti")
        sockets.connections[FakeSock()] = conn
        sockets.connections[FakeSock()] = bystander
        try:
            refreshed = await a.refresh_token(token)
            assert closed == []
            # Retargeted (#3152 review): the live connection now carries
            # the refreshed token's JTI; unrelated connections are left
            # alone.
            new_jti = a.decode_token(refreshed.access_token)["jti"]
            assert conn.jti == new_jti
            assert bystander.jti == "unrelated-jti"
        finally:
            sockets.connections.clear()

    async def test_logout_after_refresh_closes_retargeted_socket(
        self, user, app_state
    ):
        """#3152 review: the WS outlives the HTTP token refresh (it keeps
        the old, rotated JTI), so logging out with the NEW token must
        still close it — via the refresh-time retarget onto the new JTI."""
        a, sockets = self._auth_with_sockets()
        token = await self._login(a)
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        # A connection opened with the pre-refresh token, planted BEFORE
        # the refresh: the refresh retargets its JTI onto the new token.
        conn = self._fake_conn(user, a.decode_token(token)["jti"])
        sockets.connections[FakeSock()] = conn
        try:
            refreshed = await a.refresh_token(token)
            await a.logout(refreshed.access_token)
            assert closed == [(4001, "Token revoked")]
        finally:
            sockets.connections.clear()

    async def test_eviction_after_refresh_closes_retargeted_socket(
        self, user, app_state
    ):
        """#3152 review: the typical eviction victim is a long-refreshing
        session (replace_session keeps its original created_at, so it
        stays oldest) — its socket must still be kickable after rotation.
        """
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        a, sockets = self._auth_with_sockets(env)
        first = await self._login(a)
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        # The connection opened with the pre-refresh token, planted
        # BEFORE the refresh so the rotation retargets it.
        conn = self._fake_conn(user, a.decode_token(first)["jti"])
        sockets.connections[FakeSock()] = conn
        try:
            refreshed = await a.refresh_token(first)
            # The second login evicts the refreshed (still-oldest)
            # session past the cap of 1.
            second = await self._login(a)
            assert closed == [(4001, "Token revoked")]
            assert await a.get_user_from_token(second) is not None
            assert await a.get_user_from_token(refreshed.access_token) is None
        finally:
            sockets.connections.clear()

    async def test_revoke_all_user_sessions_closes_every_socket(
        self, user, app_state
    ):
        """revoke_all_user_sessions (used by change-password) blocklists
        and kicks every session, not just one."""
        a, sockets = self._auth_with_sockets()
        t1 = await self._login(a)
        t2 = await self._login(a)
        jti1 = a.decode_token(t1)["jti"]
        jti2 = a.decode_token(t2)["jti"]
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        sockets.connections[FakeSock()] = self._fake_conn(user, jti1)
        sockets.connections[FakeSock()] = self._fake_conn(user, jti2)
        try:
            await a.revoke_all_user_sessions(user["id"])
            assert len(closed) == 2
            assert all(code == 4001 for code, _ in closed)
            # Both tokens are now blocklisted.
            assert await a.get_user_from_token(t1) is None
            assert await a.get_user_from_token(t2) is None
        finally:
            sockets.connections.clear()

    async def test_revoke_all_user_sessions_no_sessions_is_noop(
        self, user, app_state
    ):
        """A user with no sessions (rows empty) skips the kick loop and
        the remove_sessions call entirely."""
        a, sockets = self._auth_with_sockets()
        await a.revoke_all_user_sessions(user["id"])
        assert not sockets.connections
        assert await a.app.state.model.sessions.list_sessions(user["id"]) == []


class TestRevocationKicksDeciders:
    """#3162: the consent-decider socket had the same revocation
    survival as the main /ws socket (#3152) — it lives in its own
    registry, so the logout/eviction kick must reach that registry too.
    Refresh rotation retargets (not closes), exactly like the main
    registry. Entries are REAL SafeWebSockets over mock raw sockets
    (#3160 review: fakes masked the reason-kwarg no-op)."""

    def _auth_with_deciders(self, env=None):
        from klangk.consent.deciders import ConsentDeciderRegistry

        a = _auth(env)
        a.app.state.consent_deciders = ConsentDeciderRegistry(a.app)
        return a

    @staticmethod
    def _decider_raw(a, jti, decider_id="d1"):
        """A real SafeWebSocket over a mock raw socket, registered as a
        live decider under *jti*; returns the raw socket."""
        from unittest.mock import AsyncMock

        from klangk.wshandler.safe_websocket import SafeWebSocket

        raw = AsyncMock()
        raw.close = AsyncMock()
        a.app.state.consent_deciders.register(
            decider_id, "ws-1", "d@x", SafeWebSocket(raw), jti=jti
        )
        return raw

    async def _login(self, a):
        result = await a.login(
            auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        return result.access_token

    async def test_logout_closes_decider_for_that_jti(self, user, app_state):
        a = self._auth_with_deciders()
        token = await self._login(a)
        jti = a.decode_token(token)["jti"]
        victim = self._decider_raw(a, jti)
        other = self._decider_raw(a, "other-jti", "d2")
        await a.logout(token)
        victim.close.assert_awaited_once_with(
            code=4001, reason="Token revoked"
        )
        other.close.assert_not_awaited()
        # The kicked decider's registration is gone (authority ends at
        # revocation); the spared one stays live.
        deciders = a.app.state.consent_deciders
        assert set(deciders._deciders) == {"d2"}

    async def test_session_limit_eviction_closes_evicted_decider(
        self, user, app_state
    ):
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        a = self._auth_with_deciders(env)
        first = await self._login(a)
        victim = self._decider_raw(a, a.decode_token(first)["jti"])
        # The second login evicts the first session past the cap of 1.
        second = await self._login(a)
        victim.close.assert_awaited_once_with(
            code=4001, reason="Token revoked"
        )
        assert await a.get_user_from_token(second) is not None

    async def test_refresh_rotation_retargets_decider_not_closed(
        self, user, app_state
    ):
        a = self._auth_with_deciders()
        token = await self._login(a)
        raw = self._decider_raw(a, a.decode_token(token)["jti"])
        refreshed = await a.refresh_token(token)
        raw.close.assert_not_awaited()
        # Retargeted onto the refreshed token's JTI, so a later hard
        # revocation still finds the decider.
        new_jti = a.decode_token(refreshed.access_token)["jti"]
        deciders = a.app.state.consent_deciders
        assert deciders._deciders["d1"]["jti"] == new_jti

    async def test_logout_after_refresh_closes_retargeted_decider(
        self, user, app_state
    ):
        a = self._auth_with_deciders()
        token = await self._login(a)
        raw = self._decider_raw(a, a.decode_token(token)["jti"])
        refreshed = await a.refresh_token(token)
        await a.logout(refreshed.access_token)
        raw.close.assert_awaited_once_with(code=4001, reason="Token revoked")

    async def test_eviction_after_refresh_closes_retargeted_decider(
        self, user, app_state
    ):
        """The typical eviction victim is a long-refreshing session
        (replace_session keeps its original created_at, so it stays
        oldest) — its decider must still be kickable after rotation."""
        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        a = self._auth_with_deciders(env)
        first = await self._login(a)
        raw = self._decider_raw(a, a.decode_token(first)["jti"])
        refreshed = await a.refresh_token(first)
        # The second login evicts the refreshed (still-oldest) session
        # past the cap of 1.
        second = await self._login(a)
        raw.close.assert_awaited_once_with(code=4001, reason="Token revoked")
        assert await a.get_user_from_token(second) is not None
        assert await a.get_user_from_token(refreshed.access_token) is None


class TestConcurrentLogonAudit:
    """Audit records for concurrent logons from different workstations
    (#2586). A workstation is the effective client IP a session was
    established from; when a login is concurrent with an active session
    from a different, known IP, an audit record is written to the
    klangk.auth logger.
    """

    def _login_req(self):
        return auth.LoginRequest(
            identifier="testuser@example.com", password="testpass"
        )

    async def _login(self, source_ip=None, user_agent=None, env=None):
        return await _auth(env).login(
            self._login_req(),
            source_ip=source_ip,
            user_agent=user_agent,
        )

    async def test_session_row_records_workstation(self, user, app_state):
        await self._login(source_ip="203.0.113.7", user_agent="klangk-cli/1.0")
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 1
        assert rows[0]["source_ip"] == "203.0.113.7"
        assert rows[0]["user_agent"] == "klangk-cli/1.0"

    async def test_workstation_defaults_to_unknown(self, user, app_state):
        """Issuance without request info (tests, internal paths) records
        NULL workstation columns — unknown, never 'different'."""
        await self._login()
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert rows[0]["source_ip"] is None
        assert rows[0]["user_agent"] is None

    async def test_different_workstation_audited(
        self, user, app_state, caplog
    ):
        """A login concurrent with an active session from another IP
        generates an audit record naming both workstations."""
        import logging

        await self._login(source_ip="203.0.113.7")
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            await self._login(source_ip="198.51.100.9")
        assert any(
            "concurrent logon from different workstations" in r.getMessage()
            and "203.0.113.7" in r.getMessage()
            and "198.51.100.9" in r.getMessage()
            for r in caplog.records
        )

    async def test_same_workstation_not_audited(self, user, app_state, caplog):
        """Two sessions from the same IP (two browsers on one machine)
        are concurrent but not from different workstations: no record."""
        import logging

        await self._login(source_ip="203.0.113.7", user_agent="ua-one")
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            await self._login(source_ip="203.0.113.7", user_agent="ua-two")
        assert not any(
            "concurrent logon" in r.getMessage() for r in caplog.records
        )

    async def test_unknown_workstation_not_audited(
        self, user, app_state, caplog
    ):
        """A session with an unknown IP never compares as 'different':
        neither direction (known→unknown nor unknown→known) audits."""
        import logging

        await self._login()  # unknown workstation
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            await self._login(source_ip="203.0.113.7")
            # Unknown again, now concurrent with a known one — inside the
            # capture context so the no-record assertion actually observes
            # this login too (review nit).
            await self._login()
        assert not any(
            "concurrent logon" in r.getMessage() for r in caplog.records
        )

    async def test_expired_other_workstation_not_audited(
        self, user, app_state, caplog
    ):
        """Dead sessions are purged before the audit check, so a login
        after another workstation's session expired generates nothing."""
        import logging

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await app_state.state.model.sessions.record_session(
            user["id"], "jti-dead", past, source_ip="203.0.113.7"
        )
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            await self._login(source_ip="198.51.100.9")
        assert not any(
            "concurrent logon" in r.getMessage() for r in caplog.records
        )

    async def test_audit_runs_before_session_limit_eviction(
        self, user, app_state, caplog
    ):
        """A cap-evicting login is still audited: the other-workstation
        session it is about to revoke was concurrent at logon time."""
        import logging

        env = {"KLANGKD_MAX_SESSIONS_PER_USER": "1"}
        await self._login(source_ip="203.0.113.7", env=env)
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            await self._login(source_ip="198.51.100.9", env=env)
        assert any(
            "concurrent logon from different workstations" in r.getMessage()
            for r in caplog.records
        )

    async def test_register_records_workstation_refresh_keeps_it(
        self, user, app_state, caplog
    ):
        """The verified-register path records the workstation on its
        session row, and a token refresh (the same session continuing)
        neither audits nor rewrites the workstation columns."""
        import logging

        result = await _auth().register(
            auth.RegisterRequest(
                email="fresh@example.com", password="longenough1"
            ),
            verified=True,
            source_ip="198.51.100.9",
            user_agent="ua-register",
        )
        rows = await app_state.state.model.sessions.list_sessions(
            result.user_id
        )
        assert rows[0]["source_ip"] == "198.51.100.9"
        assert rows[0]["user_agent"] == "ua-register"

        # Refresh continues a session; it is not a new logon (no audit
        # record) and the row keeps the logon-time workstation identity.
        with caplog.at_level(logging.INFO, logger="klangk.auth"):
            refreshed = await _auth().refresh_token(result.access_token)
        assert not any(
            "concurrent logon" in r.getMessage() for r in caplog.records
        )
        rows = await app_state.state.model.sessions.list_sessions(
            result.user_id
        )
        new_jti = _auth().decode_token(refreshed.access_token)["jti"]
        assert [r["jti"] for r in rows] == [new_jti]
        assert rows[0]["source_ip"] == "198.51.100.9"
        assert rows[0]["user_agent"] == "ua-register"


class TestDisabledAccounts:
    """#2588: disabled accounts fail auth at every choke point."""

    async def test_login_rejected(self, user, db):
        a = _auth()
        await a.app.state.model.users.set_user_disabled(user["id"], True)
        with pytest.raises(HTTPException) as exc_info:
            await a.login(
                auth.LoginRequest(
                    identifier="testuser@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 403
        assert "disabled" in exc_info.value.detail

    async def test_refresh_rejected(self, user, db):
        """A pre-disable token cannot rotate its way back in (#2588)."""
        a = _auth()
        token = a.create_token(user["id"], user["email"])
        await a.app.state.model.users.set_user_disabled(user["id"], True)
        with pytest.raises(HTTPException) as exc_info:
            await a.refresh_token(token)
        assert exc_info.value.status_code == 403

    async def test_ws_token_rejected(self, user, db):
        """get_user_from_token returns None for a disabled account — the
        WS connect rejects like any dead token."""
        a = _auth()
        token = a.create_token(user["id"], user["email"])
        await a.app.state.model.users.set_user_disabled(user["id"], True)
        assert await a.get_user_from_token(token) is None

    async def test_ensure_not_disabled_passes_enabled(self, user):
        auth.ensure_not_disabled({"disabled": False})  # no raise

    async def test_ensure_not_disabled_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            auth.ensure_not_disabled({"disabled": True})
        assert exc_info.value.status_code == 403


class TestRecordActivity:
    """#2588: throttled last_activity_at stamping."""

    async def test_stamps_and_throttles(self, user, db):
        a = _auth()
        await a.record_activity(user["id"])
        row = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row["last_activity_at"] is not None
        # A second call inside the interval skips the DB write.
        await a.record_activity(user["id"])
        row2 = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row2["last_activity_at"] == row["last_activity_at"]
        # After the interval elapses, the stamp refreshes.
        a.activity_stamps[user["id"]] -= auth.ACTIVITY_STAMP_INTERVAL
        await a.record_activity(user["id"])
        row3 = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row3["last_activity_at"] >= row["last_activity_at"]

    async def test_ws_token_path_stamps_activity(self, user, db):
        """The WS auth path stamps activity on every (unthrottled)
        authenticated lookup (#2588)."""
        a = _auth()
        token = a.create_token(user["id"], user["email"])
        assert await a.get_user_from_token(token) is not None
        row = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row["last_activity_at"] is not None

    async def test_refresh_stamps_activity(self, user, db):
        """#2588 review: a token refresh is authenticated API use — a
        headless client that only refreshes still counts as active."""
        a = _auth()
        token = a.create_token(user["id"], user["email"])
        await a.refresh_token(token)
        row = await a.app.state.model.users.get_user_by_id(user["id"])
        assert row["last_activity_at"] is not None

    async def test_forget_user_drops_stamp(self, user, db):
        """#2914: forget_user prunes the throttle entry so deleted users
        don't linger in activity_stamps for the process lifetime."""
        a = _auth()
        await a.record_activity(user["id"])
        assert user["id"] in a.activity_stamps
        a.forget_user(user["id"])
        assert user["id"] not in a.activity_stamps
        # Forgetting an id with no entry is a no-op.
        a.forget_user("never-seen")
        # After forgetting, the next call writes immediately again.
        await a.record_activity(user["id"])
        assert user["id"] in a.activity_stamps


class TestRefreshBranchGaps2834:
    """#2834 branch gate: the expired-token path without a jti claim."""

    async def test_refresh_expired_token_without_jti_returns_401(
        self, db, app_state
    ):
        # A hand-forged expired token with no jti: there is nothing to look
        # up, so the refresh fails with the plain 401 (not a KeyError).
        await app_state.state.model.users.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await app_state.state.model.users.get_user_by_email("a@b.com")
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            _auth().secret,
            algorithm=_auth().algorithm,
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().refresh_token(expired)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Token expired"


class TestRefreshTokenNoCover2910:
    async def test_missing_claims_rejected_401(self, db, app_state):
        """A structurally valid JWT lacking email/jti/exp claims is
        refused (the all([...]) guard), not refreshed."""
        from jose import jwt as _jwt

        token = _jwt.encode(
            {"sub": "uid"}, _auth().secret, algorithm=_auth().algorithm
        )
        with pytest.raises(HTTPException) as caught:
            await _auth().refresh_token(token)
        assert caught.value.status_code == 401
        assert caught.value.detail == "Invalid token"

    async def test_garbage_token_rejected_401(self, db):
        with pytest.raises(HTTPException) as caught:
            await _auth().refresh_token("not-a-jwt")
        assert caught.value.status_code == 401


class TestPasswordMinAge:
    """Minimum password age enforcement (#3177)."""

    def _auth_with_min_age(self, app_state, monkeypatch, hours=24):
        monkeypatch.setattr(
            app_state.state.settings,
            "password_min_age_hours",
            hours,
            raising=False,
        )
        return Auth(app_state)

    async def _fresh_user(self, app_state):
        pw_hash = auth.hash_password("testpass")
        return await app_state.state.model.users.create_user(
            "age@example.com", pw_hash, verified=True
        )

    async def test_disabled_when_zero(self, app_state, db):
        """Default 0 means no minimum age — never raises."""
        a = Auth(app_state)
        assert a.password_min_age_hours == 0
        user = await self._fresh_user(app_state)
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        # Should not raise even on a just-created user.
        a.validate_password_min_age(row)

    async def test_rejects_change_inside_window(
        self, app_state, db, monkeypatch
    ):
        """A password set 1 hour ago must be refused when min age is 24h."""
        a = self._auth_with_min_age(app_state, monkeypatch, hours=24)
        user = await self._fresh_user(app_state)
        # Stamp password_set_at to 1 hour ago.
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ? WHERE id = ?",
                (recent, user["id"]),
            )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_min_age(row)
        assert exc_info.value.status_code == 400
        assert "hour" in exc_info.value.detail

    async def test_allows_change_after_window(
        self, app_state, db, monkeypatch
    ):
        """A password set 25 hours ago passes a 24-hour minimum age."""
        a = self._auth_with_min_age(app_state, monkeypatch, hours=24)
        user = await self._fresh_user(app_state)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ? WHERE id = ?",
                (old, user["id"]),
            )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        a.validate_password_min_age(row)  # should not raise

    async def test_unknown_set_time_is_allowed(
        self, app_state, db, monkeypatch
    ):
        """No parseable password_set_at and no parseable created_at means
        the age cannot be judged — the check must not brick the account."""
        a = self._auth_with_min_age(app_state, monkeypatch, hours=24)
        user = await self._fresh_user(app_state)
        # created_at is NOT NULL, but it can hold an unparseable value —
        # parse_user_ts("garbage") is None, same as NULL password_set_at.
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = NULL,"
                " created_at = 'garbage' WHERE id = ?",
                (user["id"],),
            )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        a.validate_password_min_age(row)  # should not raise

    async def test_falls_back_to_created_at(self, app_state, db, monkeypatch):
        """When password_set_at is NULL, created_at is the fallback.
        A recently created account is inside the min-age window."""
        a = self._auth_with_min_age(app_state, monkeypatch, hours=24)
        user = await self._fresh_user(app_state)
        # Ensure password_set_at is NULL (migration backfill scenario).
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = NULL WHERE id = ?",
                (user["id"],),
            )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        # created_at is "now" which is within the 24h window → rejected.
        with pytest.raises(HTTPException) as exc_info:
            a.validate_password_min_age(row)
        assert exc_info.value.status_code == 400


class TestPasswordExpiry:
    """Maximum password age / expiry enforcement (#3177)."""

    def _auth_with_max_age(self, app_state, monkeypatch, days=60):
        monkeypatch.setattr(
            app_state.state.settings,
            "password_max_age_days",
            days,
            raising=False,
        )
        return Auth(app_state)

    async def _user_with_old_password(self, app_state, days_ago):
        pw_hash = auth.hash_password("testpass")
        user = await app_state.state.model.users.create_user(
            "expiry@example.com", pw_hash, verified=True
        )
        old = (
            datetime.now(timezone.utc) - timedelta(days=days_ago)
        ).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ?, created_at = ?"
                " WHERE id = ?",
                (old, old, user["id"]),
            )
        return user

    async def test_disabled_when_zero(self, app_state, db):
        """Default 0 means no expiry — password_expired always False."""
        a = Auth(app_state)
        assert a.password_max_age_days == 0
        user = await self._user_with_old_password(app_state, days_ago=999)
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        assert not a.password_expired(row)

    async def test_not_expired_inside_window(self, app_state, db, monkeypatch):
        a = self._auth_with_max_age(app_state, monkeypatch, days=60)
        user = await self._user_with_old_password(app_state, days_ago=30)
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        assert not a.password_expired(row)

    async def test_expired_past_window(self, app_state, db, monkeypatch):
        a = self._auth_with_max_age(app_state, monkeypatch, days=60)
        user = await self._user_with_old_password(app_state, days_ago=61)
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        assert a.password_expired(row)

    async def test_unknown_set_time_not_expired(
        self, app_state, db, monkeypatch
    ):
        """A local account whose timestamps cannot be parsed is never
        expired — a malformed row must not brick logins."""
        a = self._auth_with_max_age(app_state, monkeypatch, days=60)
        pw_hash = auth.hash_password("testpass")
        user = await app_state.state.model.users.create_user(
            "unknown-ts@example.com", pw_hash, verified=True
        )
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = NULL,"
                " created_at = 'garbage' WHERE id = ?",
                (user["id"],),
            )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        assert not a.password_expired(row)

    async def test_oidc_user_never_expires(self, app_state, db, monkeypatch):
        """OIDC users have no klangk password — nothing to age."""
        a = self._auth_with_max_age(app_state, monkeypatch, days=1)
        await app_state.state.model.users.create_user(
            "oidc@example.com", None, verified=True, provider="oidc"
        )
        row = await a.app.state.model.users.get_user_by_identifier(
            "oidc@example.com"
        )
        assert not a.password_expired(row)

    async def test_login_blocked_when_expired(
        self, app_state, db, monkeypatch
    ):
        """Login returns 403 with password_expired error detail."""
        a = self._auth_with_max_age(app_state, monkeypatch, days=60)
        user = await self._user_with_old_password(app_state, days_ago=61)
        with pytest.raises(HTTPException) as exc_info:
            await a.login(
                auth.LoginRequest(
                    identifier=user["email"], password="testpass"
                )
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "password_expired"

    async def test_refresh_blocked_when_expired(
        self, app_state, db, monkeypatch
    ):
        """Token refresh refuses to extend a session past the max age."""
        # Start with a non-expired password to get a valid token.
        pw_hash = auth.hash_password("testpass")
        user = await app_state.state.model.users.create_user(
            "refresh-exp@example.com", pw_hash, verified=True
        )
        a = self._auth_with_max_age(app_state, monkeypatch, days=60)
        result = await a.login(
            auth.LoginRequest(
                identifier="refresh-exp@example.com", password="testpass"
            )
        )
        # Now backdate the password to make it expired.
        old = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ?, created_at = ?"
                " WHERE id = ?",
                (old, old, user["id"]),
            )
        with pytest.raises(HTTPException) as exc_info:
            await a.refresh_token(result.access_token)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == "password_expired"


class TestChangeExpiredPassword:
    """The expired-password rotation flow (#3177)."""

    def _auth_with_expiry(self, app_state, monkeypatch, max_days=60):
        monkeypatch.setattr(
            app_state.state.settings,
            "password_max_age_days",
            max_days,
            raising=False,
        )
        return Auth(app_state)

    async def _expired_user(self, app_state, days_ago=61, verified=True):
        pw_hash = auth.hash_password("oldpass")
        user = await app_state.state.model.users.create_user(
            "rotate@example.com", pw_hash, verified=verified
        )
        old = (
            datetime.now(timezone.utc) - timedelta(days=days_ago)
        ).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "UPDATE users SET password_set_at = ?, created_at = ?"
                " WHERE id = ?",
                (old, old, user["id"]),
            )
        return user

    async def test_rotates_and_mints_token(self, app_state, db, monkeypatch):
        """Successful rotation returns a valid access token."""
        a = self._auth_with_expiry(app_state, monkeypatch)
        await self._expired_user(app_state)
        result = await a.change_expired_password(
            auth.ChangeExpiredPasswordRequest(
                identifier="rotate@example.com",
                current_password="oldpass",
                new_password="freshpass1",
            )
        )
        assert result.access_token

    async def test_rejects_when_not_expired(self, app_state, db, monkeypatch):
        """Cannot use the endpoint as a general change-password bypass."""
        a = self._auth_with_expiry(app_state, monkeypatch, max_days=60)
        pw_hash = auth.hash_password("current")
        await app_state.state.model.users.create_user(
            "notexp@example.com", pw_hash, verified=True
        )
        with pytest.raises(HTTPException) as exc_info:
            await a.change_expired_password(
                auth.ChangeExpiredPasswordRequest(
                    identifier="notexp@example.com",
                    current_password="current",
                    new_password="newpass1",
                )
            )
        assert exc_info.value.status_code == 400
        assert "not expired" in exc_info.value.detail

    async def test_rejects_wrong_current_password(
        self, app_state, db, monkeypatch
    ):
        """Wrong current password is a 401 (same gate as login)."""
        a = self._auth_with_expiry(app_state, monkeypatch)
        await self._expired_user(app_state)
        with pytest.raises(HTTPException) as exc_info:
            await a.change_expired_password(
                auth.ChangeExpiredPasswordRequest(
                    identifier="rotate@example.com",
                    current_password="wrong",
                    new_password="freshpass1",
                )
            )
        assert exc_info.value.status_code == 401

    async def test_rejects_unverified_user(self, app_state, db, monkeypatch):
        """An unverified account cannot rotate through this endpoint
        (same gate as login)."""
        a = self._auth_with_expiry(app_state, monkeypatch)
        await self._expired_user(app_state, verified=False)
        with pytest.raises(HTTPException) as exc_info:
            await a.change_expired_password(
                auth.ChangeExpiredPasswordRequest(
                    identifier="rotate@example.com",
                    current_password="oldpass",
                    new_password="freshpass1",
                )
            )
        assert exc_info.value.status_code == 403
        assert "not verified" in exc_info.value.detail

    async def test_rejects_rotation_when_min_age_exceeds_max(
        self, app_state, db, monkeypatch
    ):
        """A min > max misconfig cannot turn the expiry flow into a
        change-password / history-cycling bypass — the rotation still
        validates the minimum age."""
        monkeypatch.setattr(
            app_state.state.settings,
            "password_max_age_days",
            1,
            raising=False,
        )
        monkeypatch.setattr(
            app_state.state.settings,
            "password_min_age_hours",
            720,
            raising=False,
        )
        a = Auth(app_state)
        await self._expired_user(app_state, days_ago=2)
        with pytest.raises(HTTPException) as exc_info:
            await a.change_expired_password(
                auth.ChangeExpiredPasswordRequest(
                    identifier="rotate@example.com",
                    current_password="oldpass",
                    new_password="freshpass1",
                )
            )
        assert exc_info.value.status_code == 400
        assert "hour" in exc_info.value.detail

    async def test_password_set_at_stamped_after_rotation(
        self, app_state, db, monkeypatch
    ):
        """After a successful rotation, password_set_at is recent."""
        a = self._auth_with_expiry(app_state, monkeypatch)
        user = await self._expired_user(app_state)
        await a.change_expired_password(
            auth.ChangeExpiredPasswordRequest(
                identifier="rotate@example.com",
                current_password="oldpass",
                new_password="freshpass1",
            )
        )
        row = await a.app.state.model.users.get_user_by_identifier(
            user["email"]
        )
        assert not a.password_expired(row)
