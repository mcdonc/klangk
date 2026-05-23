"""Tests for auth module: password hashing, JWT tokens, login/register."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from bark_backend import auth, user_store


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
        await user_store.create_user(
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
        user = await user_store.create_user(
            "toverify@example.com", password_hash, verified=False
        )
        assert not user["verified"]
        result = await user_store.verify_user(user["id"])
        assert result is True
        updated = await user_store.get_user_by_email("toverify@example.com")
        assert updated["verified"] is True

    async def test_verify_nonexistent_user(self, db):
        result = await user_store.verify_user("nonexistent-id")
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
