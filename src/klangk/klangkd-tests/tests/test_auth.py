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
from klangk.auth import Auth


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
        assert h1 != h2  # bcrypt uses random salt

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
        a = _auth({"KLANGK_DISABLE_REGISTRATION": "true"})
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
        result = await _auth().register(
            auth.RegisterRequest(
                email="verified@example.com", password="pass1234"
            ),
            verified=True,
        )
        assert result.access_token
        assert result.email == "verified@example.com"

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
        a = _auth({"KLANGK_MIN_PASSWORD_LENGTH": "10"})
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
        result = await _auth().login(
            auth.LoginRequest(
                email="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token
        assert result.token_type == "bearer"

    async def test_login_wrong_password(self, user):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    email="testuser@example.com", password="wrong"
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
                    email="oidc@example.com", password="anything"
                )
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid credentials"

    async def test_login_unverified(self, db, app_state):
        import bcrypt

        password_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
        await app_state.state.model.users.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    email="unverified@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 403
        assert "not verified" in exc_info.value.detail

    async def test_login_nonexistent_user(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(email="noone@example.com", password="pass")
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
                        email="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "testuser@example.com"
            )
        )
        assert info["attempt_count"] == _auth().login_lockout_failures - 1

    async def test_login_lockout_after_max_attempts(self, user):
        """Locked out after LOGIN_LOCKOUT_FAILURES failed attempts."""
        for i in range(_auth().login_lockout_failures):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(
                        email="testuser@example.com", password="wrong"
                    )
                )
            if i < _auth().login_lockout_failures - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429
        with pytest.raises(HTTPException) as exc_info:
            await _auth().login(
                auth.LoginRequest(
                    email="testuser@example.com", password="wrong"
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
                    email="testuser@example.com", password="wrong"
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
                    email="testuser@example.com", password="wrong"
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
                email="testuser@example.com", password="testpass"
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
                    email="testuser@example.com", password="testpass"
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

    async def test_login_lockout_disabled_when_zero(self, db):
        """With login_lockout_failures=0, no rate limiting occurs."""
        a = _auth({"KLANGK_LOGIN_LOCKOUT_FAILURES": "0"})
        for _ in range(20):
            with pytest.raises(HTTPException) as exc_info:
                await a.login(
                    auth.LoginRequest(
                        email="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401

    async def test_login_nonexistent_user_also_rate_limited(self, db):
        """Nonexistent users are also rate-limited to prevent enumeration."""
        for i in range(_auth().login_lockout_failures):
            with pytest.raises(HTTPException) as exc_info:
                await _auth().login(
                    auth.LoginRequest(
                        email="nobody@example.com", password="wrong"
                    )
                )
            if i < _auth().login_lockout_failures - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429

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
                email="testuser@example.com", password="testpass"
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
        import bcrypt

        password_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
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
        a = _auth({"KLANGK_ACCESS_TOKEN_HOURS": "48"})
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
        a = _auth({"KLANGK_DISABLE_INVITES": "true"})
        assert a.invitations_enabled() is False


class TestRequireSecureJwtSecret:
    def test_secure_secret_passes(self):
        a = _auth({"KLANGK_JWT_SECRET": "a-real-strong-secret"})
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
        a = _auth({"KLANGK_PREVENT_INSECURE_JWT_SECRET": "1"})
        with pytest.raises(ConfigurationError, match="KLANGK_JWT_SECRET"):
            a.require_secure_jwt_secret()

    def test_empty_secret_blocks_with_prevent(self):
        # jwt_secret unset falls back to the insecure default; an explicit
        # empty string is also insecure and blocked when prevent is set.
        a = _auth(
            {
                "KLANGK_JWT_SECRET": "",
                "KLANGK_PREVENT_INSECURE_JWT_SECRET": "1",
            }
        )
        assert a.jwt_secret_is_secure() is False
        with pytest.raises(ConfigurationError):
            a.require_secure_jwt_secret()
