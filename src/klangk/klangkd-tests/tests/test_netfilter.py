"""Tests for the per-workspace netfilter egress-filter surface (#1365)."""

import json
import os
import stat
import types

import pytest

from klangk import netfilter as nf
from _helpers import make_settings


def _app(hooks_dir=None, tmp_path=None):
    settings = make_settings({})
    if hooks_dir is not None:
        settings.netfilter_hooks_dir = hooks_dir
    return types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )


# --- pure validators / renderers ---


class TestParseAllowedDomains:
    def test_strips_and_dedupes_preserving_order(self):
        assert nf.parse_allowed_domains(
            ["github.com:443", " github.com:443 ", "pypi.org"]
        ) == ["github.com:443", "pypi.org"]

    def test_drops_empties(self):
        assert nf.parse_allowed_domains(["", "  ", "a.com"]) == ["a.com"]

    @pytest.mark.parametrize(
        "spec",
        [
            "github.com",
            "github.com:443",
            "pypi.org:80",
            "10.0.0.1",
            "10.0.0.1:53",
            "sub.domain.example.com:8080",
            "[::1]",
            "[2001:db8::1]:443",
        ],
    )
    def test_valid_specs(self, spec):
        assert nf.parse_allowed_domains([spec]) == [spec]

    @pytest.mark.parametrize(
        "spec",
        [
            "bad spec",  # whitespace
            "a.com:abc",  # non-numeric port
            "a.com:123456",  # port too long (>5 digits)
            "/etc/passwd",  # slash
            "-leading",  # leading hyphen rejected by host grammar
            "a.com/path",
        ],
    )
    def test_invalid_specs_rejected(self, spec):
        with pytest.raises(ValueError):
            nf.parse_allowed_domains([spec])

    def test_error_lists_every_invalid_entry(self):
        with pytest.raises(ValueError) as exc:
            nf.parse_allowed_domains(["good.com", "bad spec", "also bad"])
        msg = str(exc.value)
        assert "bad spec" in msg
        assert "also bad" in msg
        assert "good.com" not in msg.split("Invalid")[1]


class TestRenderRulesAnnotation:
    def test_comma_joined(self):
        assert (
            nf.render_rules_annotation(["github.com:443", "pypi.org"])
            == "github.com:443,pypi.org"
        )

    def test_single(self):
        assert nf.render_rules_annotation(["a.com"]) == "a.com"


class TestRenderHookJson:
    def test_points_at_absolute_script_and_gates_on_annotation(self, tmp_path):
        script = str(tmp_path / "klangk-netfilter.sh")
        data = json.loads(nf.render_hook_json(script))
        assert data["hook"]["path"] == os.path.abspath(script)
        assert data["stages"] == ["createContainer"]
        # The annotations gate makes the hook fire ONLY for containers
        # that carry the annotation — an unrestricted workspace is never
        # filtered.
        assert nf.ANNOTATION_KEY in data["annotations"]


# --- NetFilter state object ---


class TestNetFilterHooksDir:
    def test_unset_returns_none(self):
        assert nf.NetFilter(_app()).hooks_dir() is None

    def test_creates_missing_dir(self, tmp_path):
        path = str(tmp_path / "nested" / "hooks")
        assert nf.NetFilter(
            _app(hooks_dir=path)
        ).hooks_dir() == os.path.realpath(path)
        assert os.path.isdir(path)

    def test_unwritable_returns_none(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        settings = make_settings({})
        settings.netfilter_hooks_dir = path

        def boom(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(os, "makedirs", boom)
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        assert nf.NetFilter(app).hooks_dir() is None


class TestNetFilterInstallHooks:
    def test_disabled_is_noop(self):
        assert nf.NetFilter(_app()).install_hooks() is None

    def test_writes_script_and_json(self, tmp_path):
        path = str(tmp_path / "hooks")
        installed = nf.NetFilter(_app(hooks_dir=path)).install_hooks()
        assert installed == os.path.realpath(path)
        script = os.path.join(path, nf.HOOK_SCRIPT_NAME)
        jsonf = os.path.join(path, nf.HOOK_JSON_NAME)
        assert os.path.isfile(script)
        # Executable so the OCI runtime can invoke it.
        mode = stat.S_IMODE(os.lstat(script).st_mode)
        assert mode & 0o111
        with open(script) as f:
            assert "klangk.netfilter.rules" in f.read()
        with open(jsonf) as f:
            data = json.load(f)
        assert data["hook"]["path"] == os.path.abspath(script)

    def test_idempotent(self, tmp_path):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        nf_obj.install_hooks()
        # Second call re-writes without error.
        assert nf_obj.install_hooks() == os.path.realpath(path)

    def test_write_failure_returns_none(self, tmp_path, monkeypatch):
        path = str(tmp_path / "hooks")
        nf_obj = nf.NetFilter(_app(hooks_dir=path))
        original_open = open

        def flaky_open(p, *a, **kw):
            if str(p).endswith(nf.HOOK_SCRIPT_NAME):
                raise OSError("disk full")
            return original_open(p, *a, **kw)

        monkeypatch.setattr("builtins.open", flaky_open)
        assert nf_obj.install_hooks() is None


class TestNetFilterCreateKwargs:
    def test_no_domains_returns_none_pair(self):
        assert nf.NetFilter(_app()).create_kwargs(None) == (None, None)
        assert nf.NetFilter(_app()).create_kwargs([]) == (None, None)

    def test_domains_without_hooks_dir_warns_and_fail_opens(self, caplog):
        app = _app()  # netfilter disabled
        with caplog.at_level("WARNING"):
            result = nf.NetFilter(app).create_kwargs(["github.com:443"])
        assert result == (None, None)
        assert any("UNRESTRICTED" in r.message for r in caplog.records)

    def test_domains_with_hooks_dir_returns_annotation_and_path(
        self, tmp_path
    ):
        path = str(tmp_path / "hooks")
        ann, hooks = nf.NetFilter(_app(hooks_dir=path)).create_kwargs(
            ["github.com:443", "pypi.org"]
        )
        assert ann == {nf.ANNOTATION_KEY: "github.com:443,pypi.org"}
        assert hooks == os.path.realpath(path)
