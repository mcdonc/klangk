"""Tests for first-run config generation (#1645 / #1607).

Bare ``klangkd`` (no ``--config``) resolves ``<KLANGK_CONFIG_DIR>/klangkd.yaml``
(default ``~/.config/klangk/klangkd.yaml``) and generates a near-empty template
on first run. No admin identity or password is emitted — the admin row is
seeded at runtime (derived from the Unix user; null password in ``none``/``oidc``
mode, or ``KLANGK_DEFAULT_PASSWORD`` in ``password``/``both`` mode — fail-fast
if unset). See ``test_main.py::TestSeedDefaultUserAuthModeGating`` for the
seeding behavior.
"""

import os
from pathlib import Path

import pytest

from klangk import first_run


class TestDefaultConfigPath:
    """default_config_path() — the path bare ``klangkd`` resolves to."""

    def test_uses_klangk_config_dir_env_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KLANGK_CONFIG_DIR", str(tmp_path))
        assert first_run.default_config_path() == str(
            tmp_path / "klangkd.yaml"
        )

    def test_falls_back_to_xdg_config_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KLANGK_CONFIG_DIR", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert first_run.default_config_path() == str(
            tmp_path / "klangk" / "klangkd.yaml"
        )

    def test_falls_back_to_home_when_xdg_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KLANGK_CONFIG_DIR", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert first_run.default_config_path() == str(
            tmp_path / ".config" / "klangk" / "klangkd.yaml"
        )

    def test_klangk_config_dir_wins_over_xdg(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit"
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("KLANGK_CONFIG_DIR", str(explicit))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert first_run.default_config_path() == str(
            explicit / "klangkd.yaml"
        )


class TestGenerateDefaultConfig:
    """generate_default_config() — writes the template + nothing else."""

    def test_writes_file_at_path(self, tmp_path):
        path = str(tmp_path / "klangkd.yaml")
        first_run.generate_default_config(path)
        assert os.path.isfile(path)

    def test_creates_parent_dir_if_missing(self, tmp_path):
        path = str(tmp_path / "nested" / "deeper" / "klangkd.yaml")
        first_run.generate_default_config(path)
        assert os.path.isfile(path)

    def test_does_not_emit_default_user_or_password(self, tmp_path):
        # The generated template carries no admin identity or password —
        # those are derived at runtime (#1645). The admin row is seeded by
        # seed_default_user (null hash in none/oidc, fail-fast in password/both).
        path = str(tmp_path / "klangkd.yaml")
        first_run.generate_default_config(path)
        body = Path(path).read_text()
        # The keys appear only as commented examples, not active config.
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("default_user:") or stripped.startswith(
                "default_password:"
            ):
                pytest.fail(
                    f"Generated file has an active (uncommented) "
                    f"admin-identity key: {stripped}"
                )

    def test_does_not_emit_config_dir_or_state_dir_keys(self, tmp_path):
        path = str(tmp_path / "klangkd.yaml")
        first_run.generate_default_config(path)
        body = Path(path).read_text()
        for line in body.splitlines():
            stripped = line.strip()
            for key in ("config_dir:", "state_dir:", "data_dir:"):
                if stripped.startswith(key):
                    pytest.fail(
                        f"Generated file has an active path key: {stripped}"
                    )

    def test_does_not_overwrite_existing_file(self, tmp_path):
        path = str(tmp_path / "klangkd.yaml")
        Path(path).write_text("product_name: operator-edited\n")
        with pytest.raises(FileExistsError):
            first_run.generate_default_config(path)
        assert "operator-edited" in Path(path).read_text()

    def test_generated_file_loads_via_klangk_settings(
        self, tmp_path, monkeypatch
    ):
        # The generated template must be parseable by KlangkSettings (that's
        # what klangkd does immediately after generation).
        path = str(tmp_path / "klangkd.yaml")
        first_run.generate_default_config(path)
        from klangk.settings import KlangkSettings

        monkeypatch.setenv("KLANGK_STATE_DIR", str(tmp_path / "state"))
        s = KlangkSettings(os.environ, config_file=path)
        # No active keys → everything is defaults. default_user is the
        # dynamic Unix-user default.
        import getpass

        assert s.default_user == f"{getpass.getuser()}@example.com"

    def test_template_mentions_solo_docs(self, tmp_path):
        # The file's purpose is discoverability — it points at the docs.
        path = str(tmp_path / "klangkd.yaml")
        first_run.generate_default_config(path)
        body = Path(path).read_text()
        assert "mcdonc.github.io/klangk" in body


class TestLauncherIntegration:
    """launcher._resolve_config_path(None) wires first-run generation in."""

    def test_none_generates_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KLANGK_CONFIG_DIR", str(tmp_path))
        from klangk.launcher import _resolve_config_path

        path = tmp_path / "klangkd.yaml"
        assert not path.is_file()
        resolved = _resolve_config_path(None)
        assert resolved == str(path)
        assert path.is_file()

    def test_none_does_not_regenerate_when_present(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KLANGK_CONFIG_DIR", str(tmp_path))
        path = tmp_path / "klangkd.yaml"
        path.write_text("product_name: already-here\n")
        from klangk.launcher import _resolve_config_path

        resolved = _resolve_config_path(None)
        assert resolved == str(path)
        assert "already-here" in path.read_text()

    def test_none_handles_fileexists_race(self, tmp_path, monkeypatch):
        # If another klangkd generates the file between our isfile check and
        # open("x"), generate_default_config raises FileExistsError. The
        # launcher must treat that as "the file is there now" and proceed,
        # not crash (a systemd restart overlap shouldn't take down the boot).
        monkeypatch.setenv("KLANGK_CONFIG_DIR", str(tmp_path))
        # Simulate the race: generate_default_config raises FileExistsError
        # (another process created the file between our isfile check and the
        # open("x") inside the generator).
        from klangk import first_run as first_run_mod

        def _raise_fileexists(path):
            raise FileExistsError(path)

        monkeypatch.setattr(
            first_run_mod, "generate_default_config", _raise_fileexists
        )
        from klangk.launcher import _resolve_config_path

        resolved = _resolve_config_path(None)
        assert resolved == str(tmp_path / "klangkd.yaml")

    def test_explicit_missing_path_still_errors(self, tmp_path):
        import typer

        from klangk.launcher import _resolve_config_path

        with pytest.raises(typer.BadParameter):
            _resolve_config_path(str(tmp_path / "does-not-exist.yaml"))

    def test_none_sentinel_still_works(self):
        from klangk.launcher import _resolve_config_path

        assert _resolve_config_path("none") == "none"

    def test_explicit_existing_path_still_works(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("product_name: test\n")
        from klangk.launcher import _resolve_config_path

        assert _resolve_config_path(str(cfg)) == str(cfg)
