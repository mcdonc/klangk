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


class TestPasswordPolicy:
    """The single-fetch /config policy parser (#2581)."""

    def test_reads_both_fields_from_one_fetch(self, monkeypatch):
        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return {
                "min_password_length": 12,
                "password_requirements": {
                    "upper": 1,
                    "lower": 2,
                    "digit": 3,
                    "special": 4,
                },
            }

        monkeypatch.setattr(account, "fetch_config", fake_fetch)
        policy = account.password_policy("http://x")
        assert policy.min_length == 12
        assert policy.requirements == {
            "upper": 1,
            "lower": 2,
            "digit": 3,
            "special": 4,
        }
        assert len(fetched) == 1  # one fetch for length + counts

    def test_defaults_when_unreachable(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: "unreachable")
        policy = account.password_policy("http://x")
        assert policy.min_length == 8
        assert policy.requirements == {
            "upper": 0,
            "lower": 0,
            "digit": 0,
            "special": 0,
        }

    def test_defaults_when_none(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: None)
        policy = account.password_policy("http://x")
        assert policy.min_length == 8
        assert policy.requirements["upper"] == 0

    def test_defaults_when_missing_fields(self, monkeypatch):
        monkeypatch.setattr(account, "fetch_config", lambda url: {})
        policy = account.password_policy("http://x")
        assert policy.min_length == 8
        assert policy.requirements["special"] == 0

    def test_defaults_when_unparseable(self, monkeypatch):
        monkeypatch.setattr(
            account,
            "fetch_config",
            lambda url: {
                "min_password_length": "abc",
                "password_requirements": {"upper": "x", "digit": 1},
            },
        )
        policy = account.password_policy("http://x")
        assert policy.min_length == 8
        assert policy.requirements == {
            "upper": 0,
            "lower": 0,
            "digit": 1,
            "special": 0,
        }

    def test_defaults_when_requirements_not_a_dict(self, monkeypatch):
        monkeypatch.setattr(
            account,
            "fetch_config",
            lambda url: {"password_requirements": "nope"},
        )
        policy = account.password_policy("http://x")
        assert policy.requirements["upper"] == 0


class TestPasswordEditDistance:
    """CLI mirror of the server's Levenshtein (#3173)."""

    def test_identical(self):
        assert account.password_edit_distance("same", "same") == 0

    def test_substitution(self):
        assert account.password_edit_distance("abc", "axc") == 1

    def test_insertion(self):
        assert account.password_edit_distance("abc", "xabc") == 1

    def test_deletion(self):
        assert account.password_edit_distance("abc", "ac") == 1

    def test_empty(self):
        assert account.password_edit_distance("", "abc") == 3
        assert account.password_edit_distance("abc", "") == 3


class TestChangedError:
    """CLI mirror of the server's min-changed gate (#3173)."""

    def test_disabled_when_zero(self):
        p = account.PasswordPolicy(
            min_length=4, requirements={}, min_changed=0
        )
        assert p.changed_error("same", "same") is None

    def test_rejects_too_similar(self):
        p = account.PasswordPolicy(
            min_length=4, requirements={}, min_changed=8
        )
        err = p.changed_error("testpass", "testpas9")
        assert err is not None
        assert "8 characters" in err

    def test_accepts_enough_change(self):
        p = account.PasswordPolicy(
            min_length=4, requirements={}, min_changed=8
        )
        assert p.changed_error("testpass", "Qwerty!234") is None


class TestParsedMinChanged:
    def test_parses_valid(self):
        assert account.parsed_min_changed({"password_min_changed": 8}) == 8

    def test_defaults_zero_when_absent(self):
        assert account.parsed_min_changed({}) == 0

    def test_defaults_zero_on_garbage(self):
        assert account.parsed_min_changed({"password_min_changed": "xyz"}) == 0

    def test_clamps_negative_to_zero(self):
        assert account.parsed_min_changed({"password_min_changed": -5}) == 0


class TestPasswordComplexityError:
    """The ASCII mirror of the server rule (stays in sync with the server
    and the Flutter PasswordPolicy — non-ASCII is special, never a letter
    or digit)."""

    _reqs = {"upper": 1, "lower": 1, "digit": 1, "special": 1}

    def _policy(self, reqs):
        return account.PasswordPolicy(min_length=4, requirements=reqs)

    def test_ok_password(self):
        err = self._policy(self._reqs).complexity_error("Aa1!aaaa")
        assert err is None

    def test_reports_every_unmet_class(self):
        err = self._policy(self._reqs).complexity_error("plain")
        assert err is not None
        assert "1 uppercase letter" in err
        assert "1 digit" in err
        assert "1 special character" in err

    def test_pluralizes_counts(self):
        reqs = {**self._reqs, "upper": 2}
        err = self._policy(reqs).complexity_error("Aa1!aaaa")
        assert "at least 2 uppercase letters" in err

    def test_zero_requirements_never_fails(self):
        zero = {"upper": 0, "lower": 0, "digit": 0, "special": 0}
        assert self._policy(zero).complexity_error("") is None

    def test_non_ascii_is_special_not_a_letter_or_digit(self):
        # Parity with the server: é is special, not lowercase; ² and ٣
        # are not digits.
        assert self._policy(self._reqs).complexity_error("Aé1!a") is None
        lower_only = {"upper": 0, "lower": 1, "digit": 0, "special": 0}
        err = self._policy(lower_only).complexity_error("éééé")
        assert "1 lowercase letter" in err
        digit_only = {"upper": 0, "lower": 0, "digit": 1, "special": 0}
        err = self._policy(digit_only).complexity_error("²٣")
        assert "1 digit" in err
