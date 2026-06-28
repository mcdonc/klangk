"""Tests for util: file- and command-backed secret resolution."""

from klangk_backend.util import (
    _read_file_value,
    _run_cmd_value,
    resolve_env_bool,
    resolve_env_secret,
    resolve_file_secret,
    sanitize_disposition_name,
)


class TestReadFileValue:
    """_read_file_value is the shared helper behind resolve_env_secret
    and resolve_file_secret."""

    def test_reads_and_strips_contents(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("from-file\n")
        contents, err = _read_file_value(f"file:{f}")
        assert contents == "from-file"
        assert err is None

    def test_missing_file_returns_error(self):
        contents, err = _read_file_value("file:/no/such/file")
        assert contents is None
        assert isinstance(err, OSError)
        assert err.filename == "/no/such/file"


class TestRunCmdValue:
    """_run_cmd_value is the cmd: counterpart of _read_file_value."""

    def test_runs_and_strips_stdout(self):
        contents, err = _run_cmd_value("cmd:printf 'from-cmd\\n'")
        assert contents == "from-cmd"
        assert err is None

    def test_pipe_and_shell_features(self):
        contents, err = _run_cmd_value("cmd:echo hello | tr a-z A-Z")
        assert contents == "HELLO"
        assert err is None

    def test_nonzero_exit_returns_error(self):
        contents, err = _run_cmd_value("cmd:false")
        assert contents is None
        assert err is not None
        assert "exited with code" in err

    def test_no_output_is_none(self):
        # A command that succeeds but prints nothing yields empty stdout,
        # which we surface as the stripped empty string (not an error).
        contents, err = _run_cmd_value("cmd:true")
        assert contents == ""
        assert err is None

    def test_timeout_returns_error(self, monkeypatch):
        import klangk_backend.util as util

        monkeypatch.setattr(util, "_CMD_TIMEOUT_SECONDS", 0.1)
        contents, err = _run_cmd_value("cmd:sleep 1")
        assert contents is None
        assert err is not None
        assert "timed out" in err

    def test_execution_failure_returns_error(self, monkeypatch):
        import klangk_backend.util as util

        def _boom(*a, **k):
            raise OSError("no shell")

        monkeypatch.setattr(util.subprocess, "run", _boom)
        contents, err = _run_cmd_value("cmd:anything")
        assert contents is None
        assert err == "no shell"


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

    def test_cmd_prefix_runs_command(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:printf 'from-cmd'")
        assert resolve_env_secret("TEST_SECRET") == "from-cmd"

    def test_cmd_prefix_with_pipe(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:echo hi | tr a-z A-Z")
        assert resolve_env_secret("TEST_SECRET") == "HI"

    def test_cmd_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("TEST_SECRET", "cmd:false")
        assert resolve_env_secret("TEST_SECRET") is None


class TestResolveEnvBool:
    def test_unset_default_false(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert resolve_env_bool("TEST_BOOL") is False

    def test_unset_default_true(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL", raising=False)
        assert resolve_env_bool("TEST_BOOL", default=True) is True

    def test_truthy_values(self, monkeypatch):
        for val in ("1", "true", "True", "YES", "yes"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert resolve_env_bool("TEST_BOOL") is True

    def test_falsy_values(self, monkeypatch):
        for val in ("0", "false", "False", "NO", "no", "maybe", ""):
            monkeypatch.setenv("TEST_BOOL", val)
            assert resolve_env_bool("TEST_BOOL") is False
            assert resolve_env_bool("TEST_BOOL", default=True) is False

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", " true ")
        assert resolve_env_bool("TEST_BOOL") is True


class TestResolveFileSecret:
    def test_plain_value(self):
        assert resolve_file_secret("plain") == "plain"

    def test_file_prefix(self, tmp_path):
        f = tmp_path / "secret"
        f.write_text("from-file\n")
        assert resolve_file_secret(f"file:{f}") == "from-file"

    def test_file_missing_returns_empty(self):
        assert resolve_file_secret("file:/no/such/file") == ""

    def test_cmd_prefix(self):
        assert resolve_file_secret("cmd:printf from-cmd") == "from-cmd"

    def test_cmd_failure_returns_empty(self):
        assert resolve_file_secret("cmd:false") == ""


class TestSanitizeDispositionName:
    def test_plain_name(self):
        assert sanitize_disposition_name("file.txt") == "file.txt"

    def test_strips_double_quotes(self):
        assert sanitize_disposition_name('f"name.txt') == "fname.txt"

    def test_replaces_slashes_with_underscore(self):
        assert sanitize_disposition_name("a/b\\c") == "a_b_c"

    def test_combined(self):
        assert sanitize_disposition_name('my/"file".txt') == "my_file.txt"
