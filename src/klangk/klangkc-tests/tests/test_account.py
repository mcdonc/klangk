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

    def test_leading_dot_rejected(self):
        assert (
            account.validate_handle(".hidden")
            == "Handle cannot start with a dot"
        )

    def test_reserved_rejected(self):
        # "work" reaches the reserved check; ".users" is caught first by the
        # leading-dot rule (same order as the server).
        msg = account.validate_handle("work")
        assert msg is not None and "reserved" in msg
        assert "dot" in account.validate_handle(".users")

    def test_too_long_beats_lowercase_check(self):
        # Server checks length before casing; the CLI copy matches that order
        # so an over-long mixed-case handle reports length, not casing.
        msg = account.validate_handle("A" * (account.MAX_HANDLE_LEN + 1))
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


class TestPasswordRequirements:
    def test_reads_from_config(self, monkeypatch):
        monkeypatch.setattr(
            account,
            "fetch_config",
            lambda url: {
                "password_requirements": {
                    "upper": 1,
                    "lower": 2,
                    "digit": 3,
                    "special": 4,
                }
            },
        )
        assert account.password_requirements("http://x") == {
            "upper": 1,
            "lower": 2,
            "digit": 3,
            "special": 4,
        }

    def test_defaults_when_missing(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: {})
        assert account.password_requirements("http://x") == {
            "upper": 0,
            "lower": 0,
            "digit": 0,
            "special": 0,
        }

    def test_defaults_when_unreachable(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: "boom")
        assert account.password_requirements("http://x") == {
            "upper": 0,
            "lower": 0,
            "digit": 0,
            "special": 0,
        }

    def test_defaults_when_not_a_dict(self, monkeypatch):
        monkeypatch.setattr(
            account,
            "fetch_config",
            lambda url: {"password_requirements": "nope"},
        )
        assert account.password_requirements("http://x")["upper"] == 0

    def test_defaults_when_unparseable(self, monkeypatch):
        monkeypatch.setattr(
            account,
            "fetch_config",
            lambda url: {"password_requirements": {"upper": "x", "digit": 1}},
        )
        reqs = account.password_requirements("http://x")
        assert reqs == {"upper": 0, "lower": 0, "digit": 1, "special": 0}


class TestPasswordComplexityError:
    _reqs = {"upper": 1, "lower": 1, "digit": 1, "special": 1}

    def test_ok_password(self):
        assert (
            account.password_complexity_error("Aa1!aaaa", self._reqs) is None
        )

    def test_reports_every_unmet_class(self):
        err = account.password_complexity_error("plain", self._reqs)
        assert err is not None
        assert "1 uppercase letter" in err
        assert "1 digit" in err
        assert "1 special character" in err

    def test_pluralizes_counts(self):
        err = account.password_complexity_error(
            "Aa1!aaaa", {**self._reqs, "upper": 2}
        )
        assert "at least 2 uppercase letters" in err

    def test_zero_requirements_never_fails(self):
        zero = {"upper": 0, "lower": 0, "digit": 0, "special": 0}
        assert account.password_complexity_error("", zero) is None
