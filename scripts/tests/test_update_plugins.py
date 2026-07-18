"""Tests for update_plugins.py — local path plugin support."""

import os
import sys


# Make sure the scripts directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import update_plugins


class TestLinkPlugin:
    def test_absolute_path(self, tmp_path):
        source = tmp_path / "my-plugin"
        source.mkdir()
        (source / "extension.ts").write_text("// test")

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": str(source)}, str(plugins_dir)
        )

        dest = plugins_dir / "my-plugin"
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)
        assert result == {"name": "my-plugin", "path": str(source)}

    def test_relative_path(self, tmp_path, monkeypatch):
        # plugins.yaml lives in tmp_path/plugins/
        yaml_dir = tmp_path / "plugins"
        yaml_dir.mkdir()
        monkeypatch.setattr(update_plugins, "YAML_PATH", str(yaml_dir / "plugins.yaml"))

        # The actual plugin source is at tmp_path/dev/my-plugin
        source = tmp_path / "dev" / "my-plugin"
        source.mkdir(parents=True)
        (source / "extension.ts").write_text("// test")

        plugins_dir = tmp_path / "installed"
        plugins_dir.mkdir()

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": "../dev/my-plugin"},
            str(plugins_dir),
        )

        dest = plugins_dir / "my-plugin"
        assert dest.is_symlink()
        assert result is not None
        assert result["name"] == "my-plugin"

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir()
        plugin_src = home / "my-plugin"
        plugin_src.mkdir()

        monkeypatch.setenv("HOME", str(home))

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": "~/my-plugin"}, str(plugins_dir)
        )

        assert result is not None
        dest = plugins_dir / "my-plugin"
        assert dest.is_symlink()

    def test_envvar_expansion(self, tmp_path, monkeypatch):
        source = tmp_path / "my-plugin"
        source.mkdir()

        monkeypatch.setenv("MY_PLUGIN_DIR", str(source))

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": "$MY_PLUGIN_DIR"},
            str(plugins_dir),
        )

        assert result is not None
        dest = plugins_dir / "my-plugin"
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)

    def test_nonexistent_path_returns_none(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        result = update_plugins.link_plugin(
            {"name": "missing", "path": str(tmp_path / "nope")},
            str(plugins_dir),
        )

        assert result is None
        assert not (plugins_dir / "missing").exists()

    def test_replaces_existing_symlink(self, tmp_path):
        old_source = tmp_path / "old"
        old_source.mkdir()
        new_source = tmp_path / "new"
        new_source.mkdir()

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        dest = plugins_dir / "my-plugin"
        os.symlink(str(old_source), str(dest))

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": str(new_source)}, str(plugins_dir)
        )

        assert result is not None
        assert os.readlink(str(dest)) == str(new_source)

    def test_replaces_existing_directory(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()

        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        dest = plugins_dir / "my-plugin"
        dest.mkdir()
        (dest / "old-file.txt").write_text("old")

        result = update_plugins.link_plugin(
            {"name": "my-plugin", "path": str(source)}, str(plugins_dir)
        )

        assert result is not None
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)


class TestMainPayloadDir:
    """main() reads the checked-in plugins.yaml and writes to --payload-dir (#1660)."""

    def test_main_reads_repo_root_yaml_writes_payload_lock(self, tmp_path, monkeypatch):
        # Fake a repo root with plugins.yaml + a plugin tree.
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "plugins.yaml").write_text(
            "plugins:\n  - name: demo\n    path: plugins/demo\n"
        )
        demo = fake_repo / "plugins" / "demo"
        demo.mkdir(parents=True)
        (demo / "extension.ts").write_text("// hi")

        # Point ROOT + YAML_PATH at the fake repo.
        monkeypatch.setattr(update_plugins, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_plugins, "YAML_PATH", str(fake_repo / "plugins.yaml")
        )

        payload = tmp_path / "payload"
        payload.mkdir()
        rc = update_plugins.main(["--payload-dir", str(payload)])

        assert rc == 0
        # The plugin is symlinked into the payload dir.
        assert (payload / "demo").is_symlink()
        # The lockfile landed in the payload dir (not next to plugins.yaml).
        lock = payload / "plugins.lock"
        assert lock.is_file()
        import yaml

        data = yaml.safe_load(lock.read_text())
        assert data["plugins"][0]["name"] == "demo"

    def test_main_ignores_klangk_plugins_dir_env(self, tmp_path, monkeypatch):
        # KLANGK_PLUGINS_DIR must have no effect on where the payload lands
        # (#1660 — the var is gone from every layer).
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "plugins.yaml").write_text("plugins: []\n")
        monkeypatch.setattr(update_plugins, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_plugins, "YAML_PATH", str(fake_repo / "plugins.yaml")
        )

        bogus = tmp_path / "bogus-env-target"
        bogus.mkdir()
        monkeypatch.setenv("KLANGK_PLUGINS_DIR", str(bogus))

        payload = tmp_path / "payload"
        payload.mkdir()
        rc = update_plugins.main(["--payload-dir", str(payload)])

        assert rc == 0
        # Nothing was written under the env-var-named dir.
        assert not (bogus / "plugins.lock").exists()
        # The module no longer references the env var at all.
        assert "KLANGK_PLUGINS_DIR" not in {
            name for name, _ in update_plugins.__dict__.items()
        }

    def test_main_errors_when_plugins_yaml_missing(self, tmp_path, monkeypatch):
        # No checked-in plugins.yaml -> clear error, not silent template creation
        # (the old first-run bootstrap is gone; the file is source-controlled).
        empty_repo = tmp_path / "repo"
        empty_repo.mkdir()
        monkeypatch.setattr(update_plugins, "ROOT", str(empty_repo))
        monkeypatch.setattr(
            update_plugins, "YAML_PATH", str(empty_repo / "plugins.yaml")
        )
        rc = update_plugins.main(["--payload-dir", str(tmp_path / "payload")])
        assert rc == 1
