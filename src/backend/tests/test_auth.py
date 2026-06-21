"""Tests for auth module: password hashing, JWT tokens, login/register."""

import os
import pytest
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from klangk_backend import auth, model
from klangk_backend.exceptions import ConfigurationError


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

    def test_hash_password_accepts_exactly_72_bytes(self):
        pw = "a" * 72
        hashed = auth.hash_password(pw)
        assert auth.verify_password(pw, hashed)


class TestValidatePasswordLength:
    def test_rejects_short_password(self):
        with pytest.raises(HTTPException) as exc_info:
            auth.validate_password_length("")
        assert exc_info.value.status_code == 400
        assert "at least" in exc_info.value.detail

    def test_rejects_over_72_bytes(self):
        with pytest.raises(HTTPException) as exc_info:
            auth.validate_password_length("a" * 73)
        assert exc_info.value.status_code == 400
        assert "72 bytes" in exc_info.value.detail

    def test_rejects_multibyte_over_72_bytes(self):
        pw = "\u00e9" * 37  # 2 bytes each = 74 bytes
        with pytest.raises(HTTPException) as exc_info:
            auth.validate_password_length(pw)
        assert exc_info.value.status_code == 400

    def test_accepts_valid_password(self):
        auth.validate_password_length("goodpass")


class TestJWT:
    def test_create_and_decode_token(self):
        token = auth.create_token("user-123", "alice@example.com")
        payload = auth.decode_token(token)
        assert payload["sub"] == "user-123"
        assert payload["email"] == "alice@example.com"
        assert "jti" in payload
        assert "exp" in payload

    def test_invalid_token_raises(self):
        from jose import JWTError

        with pytest.raises(JWTError):
            auth.decode_token("garbage.token.value")


class TestRegister:
    async def test_register_disabled(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DISABLE_REGISTRATION", "true")
        with pytest.raises(HTTPException) as exc_info:
            await auth.register(
                auth.RegisterRequest(
                    email="blocked@example.com", password="pass1234"
                )
            )
        assert exc_info.value.status_code == 403

    async def test_register_success(self, db):
        result = await auth.register(
            auth.RegisterRequest(email="new@example.com", password="pass1234")
        )
        assert result.user_id
        assert result.email == "new@example.com"
        assert result.access_token is None  # unverified, no token

    async def test_register_verified(self, db):
        result = await auth.register(
            auth.RegisterRequest(
                email="verified@example.com", password="pass1234"
            ),
            verified=True,
        )
        assert result.access_token
        assert result.email == "verified@example.com"

    async def test_register_duplicate_email(self, db):
        await auth.register(
            auth.RegisterRequest(email="dup@example.com", password="pass1234")
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.register(
                auth.RegisterRequest(
                    email="dup@example.com", password="pass5678"
                )
            )
        assert exc_info.value.status_code == 400

    async def test_register_invalid_email(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await auth.register(
                auth.RegisterRequest(email="not-an-email", password="pass1234")
            )
        assert exc_info.value.status_code == 400

    async def test_register_short_password(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await auth.register(
                auth.RegisterRequest(email="valid@example.com", password="abc")
            )
        assert exc_info.value.status_code == 400

    async def test_register_password_length_configurable(
        self, db, monkeypatch
    ):
        """MIN_PASSWORD_LENGTH can be overridden via env var."""
        monkeypatch.setattr(auth, "MIN_PASSWORD_LENGTH", 8)
        with pytest.raises(HTTPException) as exc_info:
            await auth.register(
                auth.RegisterRequest(
                    email="valid@example.com", password="1234567"
                )
            )
        assert exc_info.value.status_code == 400
        assert (
            "8" in exc_info.value.detail
        )  # error message includes the length

        # 8 chars should succeed
        result = await auth.register(
            auth.RegisterRequest(
                email="valid@example.com", password="12345678"
            )
        )
        assert result.user_id


class TestLogin:
    async def test_login_success(self, user):
        result = await auth.login(
            auth.LoginRequest(
                email="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token
        assert result.token_type == "bearer"

    async def test_login_wrong_password(self, user):
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(
                    email="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 401

    async def test_login_unverified(self, db):
        import bcrypt

        password_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
        await model.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(
                    email="unverified@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 403
        assert "not verified" in exc_info.value.detail

    async def test_login_nonexistent_user(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(email="noone@example.com", password="pass")
            )
        assert exc_info.value.status_code == 401


class TestLoginRateLimit:
    """ "Tests for login brute-force protection.

    These require KLANGK_LOGIN_LOCKOUT_FAILURES > 0 (default is 0 = disabled),
    so the class setup/teardown temporarily sets it to 5 and reloads
    the auth module.
    """

    def setup_method(self):
        self._prev = os.environ.get("KLANGK_LOGIN_LOCKOUT_FAILURES")
        os.environ["KLANGK_LOGIN_LOCKOUT_FAILURES"] = "5"
        import importlib
        import klangk_backend.auth as a

        importlib.reload(a)
        globals()["auth"] = a
        globals()["model"] = a.model

    def teardown_method(self):
        if self._prev is None:
            os.environ.pop("KLANGK_LOGIN_LOCKOUT_FAILURES", None)
        else:
            os.environ["KLANGK_LOGIN_LOCKOUT_FAILURES"] = self._prev
        import importlib
        import klangk_backend.auth as a

        importlib.reload(a)
        globals()["auth"] = a
        globals()["model"] = a.model

    async def test_login_wrong_password_records_attempt(self, user):
        """Wrong password increments attempt count."""
        for i in range(auth.LOGIN_LOCKOUT_FAILURES - 1):
            with pytest.raises(HTTPException) as exc_info:
                await auth.login(
                    auth.LoginRequest(
                        email="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401
        info = await model.get_login_attempt_info("testuser@example.com")
        assert info["attempt_count"] == auth.LOGIN_LOCKOUT_FAILURES - 1

    async def test_login_lockout_after_max_attempts(self, user):
        """Locked out after LOGIN_LOCKOUT_FAILURES failed attempts."""
        for i in range(auth.LOGIN_LOCKOUT_FAILURES):
            with pytest.raises(HTTPException) as exc_info:
                await auth.login(
                    auth.LoginRequest(
                        email="testuser@example.com", password="wrong"
                    )
                )
            if i < auth.LOGIN_LOCKOUT_FAILURES - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(
                    email="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 429

    async def test_login_lockout_message_shows_remaining_time(self, user):
        """Lockout message includes remaining minutes."""
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        await model.record_failed_login("testuser@example.com")
        await model.set_login_lockout(
            "testuser@example.com", locked_until.isoformat()
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(
                    email="testuser@example.com", password="wrong"
                )
            )
        assert exc_info.value.status_code == 429
        assert "minutes" in exc_info.value.detail

    async def test_expired_lockout_allows_login(self, user):
        """An expired lockout doesn't block the user from logging in."""
        expired_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        await model.record_failed_login("testuser@example.com")
        await model.set_login_lockout(
            "testuser@example.com", expired_until.isoformat()
        )
        result = await auth.login(
            auth.LoginRequest(
                email="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token

    async def test_login_blocked_while_lockout_active(self, user):
        """Active lockout returns 429 with a countdown."""
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        await model.record_failed_login("testuser@example.com")
        await model.set_login_lockout(
            "testuser@example.com", locked_until.isoformat()
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.login(
                auth.LoginRequest(
                    email="testuser@example.com", password="testpass"
                )
            )
        assert exc_info.value.status_code == 429

    async def test_should_lockout_helper(self, db):
        """_should_lockout returns True at threshold, False below/above."""
        assert auth._should_lockout({"attempt_count": 4}) is False
        assert auth._should_lockout({"attempt_count": 5}) is True
        assert auth._should_lockout(None) is False

    async def test_should_lockout_respects_configured_threshold(self, db):
        """_should_lockout uses LOGIN_LOCKOUT_FAILURES as the threshold."""
        assert auth._should_lockout({"attempt_count": 5}) is True
        assert auth._should_lockout({"attempt_count": 4}) is False

    async def test_is_locked_out_helper(self, db):
        """_is_locked_out returns True/msg when locked_until is in the future."""
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        locked = {"attempt_count": 5, "locked_until": future.isoformat()}
        is_locked, msg = auth._is_locked_out(locked)
        assert is_locked is True
        assert "minutes" in msg
        expired = {"attempt_count": 5, "locked_until": past.isoformat()}
        is_locked2, msg2 = auth._is_locked_out(expired)
        assert is_locked2 is False
        assert msg2 is None
        no_lock = {"attempt_count": 1, "locked_until": None}
        assert auth._is_locked_out(no_lock) == (False, None)
        assert auth._is_locked_out(None) == (False, None)

    async def test_login_lockout_disabled_when_zero(self, db, monkeypatch):
        """With LOGIN_LOCKOUT_FAILURES=0, no rate limiting occurs."""
        monkeypatch.setattr(auth, "LOGIN_LOCKOUT_FAILURES", 0)
        for _ in range(20):
            with pytest.raises(HTTPException) as exc_info:
                await auth.login(
                    auth.LoginRequest(
                        email="testuser@example.com", password="wrong"
                    )
                )
            assert exc_info.value.status_code == 401

    async def test_login_nonexistent_user_also_rate_limited(self, db):
        """Nonexistent users are also rate-limited to prevent enumeration."""
        for i in range(auth.LOGIN_LOCKOUT_FAILURES):
            with pytest.raises(HTTPException) as exc_info:
                await auth.login(
                    auth.LoginRequest(
                        email="nobody@example.com", password="wrong"
                    )
                )
            if i < auth.LOGIN_LOCKOUT_FAILURES - 1:
                assert exc_info.value.status_code == 401
            else:
                assert exc_info.value.status_code == 429

    async def test_login_clears_attempts(self, db, user):
        """Successful login clears failed attempt counts."""
        await model.record_failed_login("testuser@example.com")
        await model.record_failed_login("testuser@example.com")
        result = await auth.login(
            auth.LoginRequest(
                email="testuser@example.com", password="testpass"
            )
        )
        assert result.access_token
        info = await model.get_login_attempt_info("testuser@example.com")
        assert info is None


class TestVerification:
    def test_create_and_decode_verification_token(self):
        token = auth.create_verification_token("user-123")
        user_id = auth.decode_verification_token(token)
        assert user_id == "user-123"

    def test_decode_invalid_token(self):
        assert auth.decode_verification_token("garbage") is None

    def test_decode_wrong_purpose(self):
        # A regular auth token should not pass as a verification token
        token = auth.create_token("user-123", "test")
        assert auth.decode_verification_token(token) is None

    async def test_verify_user(self, db):
        import bcrypt

        password_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
        user = await model.create_user(
            "toverify@example.com", password_hash, verified=False
        )
        assert not user["verified"]
        result = await model.verify_user(user["id"])
        assert result is True
        updated = await model.get_user_by_email("toverify@example.com")
        assert updated["verified"] is True

    async def test_verify_nonexistent_user(self, db):
        result = await model.verify_user("nonexistent-id")
        assert result is False


class TestPasswordReset:
    def test_create_and_decode_reset_token(self):
        token = auth.create_password_reset_token("user-456")
        assert auth.decode_password_reset_token(token) == "user-456"

    def test_decode_invalid_token(self):
        assert auth.decode_password_reset_token("garbage") is None

    def test_reset_and_verify_tokens_not_interchangeable(self):
        reset = auth.create_password_reset_token("user-456")
        verify = auth.create_verification_token("user-456")
        assert auth.decode_verification_token(reset) is None
        assert auth.decode_password_reset_token(verify) is None


class TestWorkspaceToken:
    def test_create_and_decode_workspace_token(self):
        token = auth.create_workspace_token("ws-123")
        assert auth.decode_workspace_token(token) == "ws-123"

    def test_decode_invalid_token(self):
        assert auth.decode_workspace_token("garbage") is None

    def test_user_token_rejected(self):
        user_token = auth.create_token("user-1", "u@test.com")
        assert auth.decode_workspace_token(user_token) is None

    def test_verify_token_rejected(self):
        verify_token = auth.create_verification_token("user-1")
        assert auth.decode_workspace_token(verify_token) is None

    def test_workspace_token_rejected_by_other_decoders(self):
        ws_token = auth.create_workspace_token("ws-123")
        assert auth.decode_verification_token(ws_token) is None
        assert auth.decode_password_reset_token(ws_token) is None


class TestTokenValidation:
    async def test_get_user_from_valid_token(self, user):
        token = auth.create_token(user["id"], user["email"])
        result = await auth.get_user_from_token(token)
        assert result is not None
        assert result["id"] == user["id"]

    async def test_get_user_from_invalid_token(self, db):
        result = await auth.get_user_from_token("invalid.token.here")
        assert result is None

    async def test_blocklisted_token_rejected(self, user):
        token = auth.create_token(user["id"], user["email"])
        # Token should work before blocklisting
        assert await auth.get_user_from_token(token) is not None
        # Blocklist it
        await auth.logout(token)
        # Now it should fail
        assert await auth.get_user_from_token(token) is None

    async def test_get_user_from_token_missing_sub(self, db):
        """Token with no 'sub' claim returns None."""
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        assert await auth.get_user_from_token(token) is None

    async def test_get_user_from_token_missing_jti(self, db):
        """Token with no 'jti' claim returns None."""
        token = jwt.encode(
            {"sub": "uid", "email": "x", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        assert await auth.get_user_from_token(token) is None

    async def test_get_user_from_token_deleted_user(self, user):
        """Token for a user that no longer exists returns None."""
        token = auth.create_token("nonexistent-id", "ghost@example.com")
        assert await auth.get_user_from_token(token) is None


class TestGetCurrentUser:
    async def test_valid_credentials(self, user):
        token = auth.create_token(user["id"], user["email"])
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user(creds)
        assert result["id"] == user["id"]

    async def test_no_credentials(self, db):
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(None)
        assert exc_info.value.status_code == 401

    async def test_invalid_token(self, db):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="bad.token.here"
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(creds)
        assert exc_info.value.status_code == 401

    async def test_missing_sub_in_token(self, db):
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(creds)
        assert exc_info.value.status_code == 401

    async def test_blocklisted_token(self, user):
        token = auth.create_token(user["id"], user["email"])
        await auth.logout(token)
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(creds)
        assert exc_info.value.status_code == 401

    async def test_deleted_user(self, user):
        token = auth.create_token("nonexistent-id", "ghost@example.com")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.get_current_user(creds)
        assert exc_info.value.status_code == 401


class TestGetCurrentUserOptional:
    async def test_valid_credentials(self, user):
        token = auth.create_token(user["id"], user["email"])
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(creds)
        assert result is not None
        assert result["id"] == user["id"]

    async def test_no_credentials(self, db):
        result = await auth.get_current_user_optional(None)
        assert result is None

    async def test_invalid_token(self, db):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="bad.token"
        )
        result = await auth.get_current_user_optional(creds)
        assert result is None

    async def test_missing_sub(self, db):
        token = jwt.encode(
            {"email": "x", "jti": "j1", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(creds)
        assert result is None

    async def test_blocklisted_token(self, user):
        token = auth.create_token(user["id"], user["email"])
        await auth.logout(token)
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(creds)
        assert result is None

    async def test_deleted_user(self, user):
        token = auth.create_token("nonexistent-id", "ghost@example.com")
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=token
        )
        result = await auth.get_current_user_optional(creds)
        assert result is None


class TestLogout:
    async def test_logout_invalid_token(self, db):
        """Logout with garbage token should not raise."""
        await auth.logout("not.a.valid.token")

    async def test_logout_token_without_jti(self, db):
        """Logout with token missing jti should not raise."""
        token = jwt.encode(
            {"sub": "uid", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        await auth.logout(token)


class TestRefreshToken:
    async def test_refresh_returns_new_token(self, db):
        """Refreshing a valid token returns a new token."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        token = auth.create_token(user["id"], user["email"])
        result = await auth.refresh_token(token)
        assert result.access_token != token
        # Old JTI should be blocklisted
        old_payload = auth.decode_token(token, allow_expired=True)
        assert await model.is_token_blocklisted(old_payload["jti"])

    async def test_refresh_idempotent(self, db):
        """Refreshing the same token twice returns the same new token."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        token = auth.create_token(user["id"], user["email"])
        result1 = await auth.refresh_token(token)
        result2 = await auth.refresh_token(token)
        assert result1.access_token == result2.access_token

    async def test_refresh_expired_token_returns_401(self, db):
        """Refreshing an expired token with no prior refresh returns 401."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "expired-jti",
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.refresh_token(expired)
        assert exc_info.value.status_code == 401

    async def test_refresh_expired_token_with_prior_refresh(self, db):
        """Refreshing an expired token returns cached new token if within window."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        # Simulate a token that was refreshed, then expired
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        await model.blocklist_token(
            "old-jti", expires_at, new_token="cached-new-token"
        )
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "old-jti",
                "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
            },
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        result = await auth.refresh_token(expired)
        assert result.access_token == "cached-new-token"

    async def test_refresh_expired_returns_cached_regardless_of_blocklist_expiry(
        self, db
    ):
        """Cached replacement is returned even when the old token's
        blocklist expires_at has passed — the new token's own exp
        governs its validity."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        expires_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        await model.blocklist_token(
            "old-jti", expires_at, new_token="cached-replacement"
        )
        expired = jwt.encode(
            {
                "sub": user["id"],
                "email": user["email"],
                "jti": "old-jti",
                "exp": datetime.now(timezone.utc) - timedelta(hours=2),
            },
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        result = await auth.refresh_token(expired)
        assert result.access_token == "cached-replacement"

    async def test_refresh_deleted_user_returns_401(self, db):
        """Refreshing a token for a deleted user returns 401."""
        token = auth.create_token("nonexistent-user", "gone@example.com")
        with pytest.raises(HTTPException) as exc_info:
            await auth.refresh_token(token)
        assert exc_info.value.status_code == 401

    async def test_refresh_revoked_token_returns_401(self, db):
        """Refreshing a revoked (logged out) token returns 401."""
        await model.create_user(
            "a@b.com", auth.hash_password("pw"), verified=True
        )
        user = await model.get_user_by_email("a@b.com")
        token = auth.create_token(user["id"], user["email"])
        await auth.logout(token)
        with pytest.raises(HTTPException) as exc_info:
            await auth.refresh_token(token)
        assert exc_info.value.status_code == 401

    def test_configurable_token_expire_hours(self, monkeypatch):
        """TOKEN_EXPIRE_HOURS reads from KLANGK_ACCESS_TOKEN_HOURS."""
        monkeypatch.setenv("KLANGK_ACCESS_TOKEN_HOURS", "48")
        # Re-evaluate the module-level constant
        result = int(
            auth.resolve_env_secret("KLANGK_ACCESS_TOKEN_HOURS", "24")
        )
        assert result == 48


class TestInvitationTokens:
    def test_roundtrip(self):
        token = auth.create_invitation_token("inv-123", "user@example.com")
        result = auth.decode_invitation_token(token)
        assert result == ("inv-123", "user@example.com")

    def test_wrong_purpose_rejected(self):
        token = auth.create_verification_token("uid")
        assert auth.decode_invitation_token(token) is None

    def test_invalid_token_returns_none(self):
        assert auth.decode_invitation_token("garbage") is None

    def test_missing_email_returns_none(self):
        token = jwt.encode(
            {"sub": "inv-1", "purpose": "invite", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        assert auth.decode_invitation_token(token) is None

    def test_missing_sub_returns_none(self):
        token = jwt.encode(
            {"email": "x@y.com", "purpose": "invite", "exp": 9999999999},
            auth.SECRET_KEY,
            algorithm=auth.ALGORITHM,
        )
        assert auth.decode_invitation_token(token) is None


class TestInvitationsEnabled:
    def test_enabled_by_default(self):
        assert auth.invitations_enabled() is True

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("KLANGK_DISABLE_INVITES", "true")
        assert auth.invitations_enabled() is False


class TestRequireSecureJwtSecret:
    def test_secure_secret_passes(self, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_KEY", "a-real-strong-secret")
        assert auth.jwt_secret_is_secure() is True
        auth.require_secure_jwt_secret()  # no raise

    def test_default_secret_is_insecure(self, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_KEY", auth._INSECURE_DEFAULT_SECRET)
        assert auth.jwt_secret_is_secure() is False

    def test_default_secret_warns(self, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_KEY", auth._INSECURE_DEFAULT_SECRET)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        auth.require_secure_jwt_secret()  # warns but does not raise

    def test_default_secret_blocks_with_prevent(self, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_KEY", auth._INSECURE_DEFAULT_SECRET)
        monkeypatch.setenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", "1")
        with pytest.raises(ConfigurationError, match="KLANGK_JWT_SECRET"):
            auth.require_secure_jwt_secret()

    def test_empty_secret_blocks_with_prevent(self, monkeypatch):
        monkeypatch.setattr(auth, "SECRET_KEY", "")
        monkeypatch.setenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", "1")
        assert auth.jwt_secret_is_secure() is False
        with pytest.raises(ConfigurationError):
            auth.require_secure_jwt_secret()
