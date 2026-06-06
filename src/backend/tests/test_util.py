"""Tests for util: file-backed secret resolution."""

from klangk_backend.util import resolve_env_secret, resolve_file_secret


class TestResolveEnvSecret:
    def test_plain_value(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "plain-value")
        assert resolve_env_secret("TEST_SECRET") == "plain-value"

    def test_file_prefix_reads_file(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("from-file\n")
        monkeypatch.setenv("TEST_SECRET", f"file:{secret_file}")
        assert resolve_env_secret("TEST_SECRET") == "from-file"

    def test_file_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "file:/no/such/file")
        assert resolve_env_secret("TEST_SECRET") is None

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert resolve_env_secret("TEST_SECRET") is None

    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("TEST_SECRET", raising=False)
        assert resolve_env_secret("TEST_SECRET", "fallback") == "fallback"

    def test_empty_string_returned_as_is(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "")
        assert resolve_env_secret("TEST_SECRET") == ""


class TestResolveFileSecret:
    def test_plain_value(self):
        assert resolve_file_secret("plain") == "plain"

    def test_file_prefix(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("from-file\n")
        assert resolve_file_secret(f"file:{f}") == "from-file"

    def test_file_missing_returns_empty(self):
        assert resolve_file_secret("file:/no/such/file") == ""
