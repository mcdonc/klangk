"""Tests for klangk.cli.account (shared account-self-service validation)."""

from __future__ import annotations

import pytest

from klangk.cli import account


class TestValidateHandle:
    @pytest.mark.parametrize(
        "handle",
        ["me", "me.too", "me-too", "me_too", "a1", "x" * 32],
    )
    def test_valid(self, handle):
        assert account.validate_handle(handle) is None

    @pytest.mark.parametrize(
        "handle",
        ["", "   "],
    )
    def test_empty(self, handle):
        assert account.validate_handle(handle) == "Handle cannot be empty"

    def test_uppercase_rejected(self):
        assert account.validate_handle("Me") == "Handle must be lowercase"

    def test_invalid_chars_rejected(self):
        assert "lowercase letters" in account.validate_handle("me!")

    def test_too_long(self):
        msg = account.validate_handle("a" * (account.MAX_HANDLE_LEN + 1))
        assert msg is not None and "characters" in msg

    def test_strips_whitespace(self):
        # Surrounding whitespace is trimmed before checking.
        assert account.validate_handle("  me  ") is None


class TestValidateEmail:
    @pytest.mark.parametrize(
        "email",
        ["a@b.com", "me.you@sub.example.org", "x+tag@y.co"],
    )
    def test_valid(self, email):
        assert account.validate_email(email) is None

    @pytest.mark.parametrize(
        "email",
        ["", "   ", "notanemail", "a@b", "a b@c.com", "@c.com", "a@.com"],
    )
    def test_invalid(self, email):
        assert account.validate_email(email) is not None


class TestPasswordMinLength:
    def test_reads_from_config(self, monkeypatch):
        monkeypatch.setattr(
            account, "fetch_config", lambda url: {"min_password_length": 12}
        )
        assert account.password_min_length("http://x") == 12

    def test_defaults_when_unreachable(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: "unreachable")
        assert account.password_min_length("http://x") == 8

    def test_defaults_when_none(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: None)
        assert account.password_min_length("http://x") == 8

    def test_defaults_when_missing_field(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: {})
        assert account.password_min_length("http://x") == 8

    def test_defaults_when_unparseable(self, monkeypatch):
        monkeypatch.setattr(
            account, "fetch_config", lambda url: {"min_password_length": "abc"}
        )
        assert account.password_min_length("http://x") == 8
