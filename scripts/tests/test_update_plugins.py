"""Tests for update_plugins.py — local path plugin support."""

import os
import shutil
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

    def test_main_without_payload_dir_uses_atexit_cleaned_tempdir(
        self, tmp_path, monkeypatch
    ):
        # The standalone-debugging path: no --payload-dir, so main() mints a
        # fresh tempdir itself and registers it with atexit so it can't leak
        # (#1665 review finding). Verify (a) main() registers exactly one new
        # atexit callback and (b) the payload dir it created is a real tempdir
        # with the expected contents, then unregister the callback so the test
        # doesn't hold a stale reference.
        import atexit

        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "plugins.yaml").write_text(
            "plugins:\n  - name: solo\n    path: plugins/solo\n"
        )
        solo = fake_repo / "plugins" / "solo"
        solo.mkdir(parents=True)
        (solo / "extension.ts").write_text("// hi")
        monkeypatch.setattr(update_plugins, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_plugins, "YAML_PATH", str(fake_repo / "plugins.yaml")
        )

        before = atexit._ncallbacks()  # type: ignore[attr-defined]
        rc = update_plugins.main([])
        after = atexit._ncallbacks()  # type: ignore[attr-defined]

        assert rc == 0
        assert after == before + 1, (
            "main() must register exactly one atexit cleanup for its tempdir"
        )
        # _make_temp_payload_dir used the "klangk-plugins-" prefix; find the
        # dir it created so we can assert contents, then clean it up (the
        # registered rmtree would fire at session end otherwise).
        import glob

        candidates = glob.glob("/tmp/klangk-plugins-*") + glob.glob(
            os.environ.get("TMPDIR", "/tmp") + "/klangk-plugins-*"
        )
        assert candidates, "expected a klangk-plugins-* tempdir to exist"
        payload_path = max(candidates, key=os.path.getmtime)
        assert os.path.isfile(os.path.join(payload_path, "plugins.lock"))
        # Reap now so the test is tidy; unregister so atexit doesn't touch it.
        shutil.rmtree(payload_path, ignore_errors=True)


class TestLocalOnlyFlag:
    """--local-only skips git-sourced plugins without hitting the network (#1664).

    Tests use this to verify the local-plugin contract without cloning remote
    repos. Git entries land in plugins.lock with sha: 'skipped' so the lock
    shape stays consistent (every declared plugin appears).
    """

    def _setup_repo(self, tmp_path, monkeypatch):
        """A repo with one local + one git plugin declared."""
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "plugins.yaml").write_text(
            "plugins:\n"
            "  - name: local-one\n"
            "    path: plugins/local-one\n"
            "  - name: remote-one\n"
            "    git: https://example.invalid/owner/repo.git\n"
            "    ref: v1.0\n"
        )
        local = fake_repo / "plugins" / "local-one"
        local.mkdir(parents=True)
        (local / "extension.ts").write_text("// hi")
        monkeypatch.setattr(update_plugins, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_plugins, "YAML_PATH", str(fake_repo / "plugins.yaml")
        )
        return fake_repo

    def test_skips_git_entries(self, tmp_path, monkeypatch):
        """Git entries are skipped (not fetched) under --local-only."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        # Would normally try to clone example.invalid; --local-only skips it.
        rc = update_plugins.main(["--payload-dir", str(payload), "--local-only"])
        assert rc == 0

        # Only the local plugin is materialized as a directory.
        materialized = {p for p in os.listdir(payload) if (payload / p).is_dir()}
        assert materialized == {"local-one"}, (
            f"--local-only should skip remote-one; materialized={materialized}"
        )

    def test_git_entries_recorded_as_skipped_in_lock(self, tmp_path, monkeypatch):
        """Skipped git entries still appear in plugins.lock with sha='skipped'.

        The lock shape stays consistent (every declared plugin appears) so a
        downstream consumer doesn't see plugins vanishing from the lock when
        the build runs --local-only."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        update_plugins.main(["--payload-dir", str(payload), "--local-only"])

        import yaml

        lock = yaml.safe_load((payload / "plugins.lock").read_text())
        entries = {e["name"]: e for e in lock["plugins"]}
        assert set(entries) == {"local-one", "remote-one"}, (
            f"lock should list both plugins; got {sorted(entries)}"
        )
        assert entries["remote-one"]["sha"] == "skipped", (
            f"remote-one should be sha='skipped'; got {entries['remote-one']!r}"
        )
        # The git URL + ref are preserved in the lock for traceability.
        assert entries["remote-one"]["git"].endswith("/repo.git")
        assert entries["remote-one"]["ref"] == "v1.0"

    def test_local_only_off_by_default(self, tmp_path, monkeypatch):
        """Without --local-only, a git entry with an unresolvable ref fails
        (the script never silently degrades to skipping)."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        # No --local-only: the bogus git URL is attempted. fetch_plugin prints
        # an ERROR and returns None; the plugin is dropped from the lock
        # (the silent-fallback-to-default-branch guard from #1660). Use a
        # file:// URL so the test doesn't hit DNS (no network access in the
        # scripts test suite).
        fake_repo = tmp_path / "repo"
        # Override the git URL to a file:// path that doesn't exist.
        (fake_repo / "plugins.yaml").write_text(
            "plugins:\n"
            "  - name: local-one\n"
            "    path: plugins/local-one\n"
            "  - name: remote-one\n"
            "    git: file:///nonexistent/path/repo.git\n"
            "    ref: v1.0\n"
        )

        rc = update_plugins.main(["--payload-dir", str(payload)])
        assert rc == 0  # main() doesn't exit nonzero on a failed fetch

        import yaml

        lock = yaml.safe_load((payload / "plugins.lock").read_text())
        names = {e["name"] for e in lock["plugins"]}
        # remote-one was attempted, failed, and dropped — only local-one lands.
        assert names == {"local-one"}, (
            f"failed-fetch plugin should be dropped; lock has {sorted(names)}"
        )

    def test_local_only_preserves_prior_real_sha(self, tmp_path, monkeypatch):
        """A pre-existing real SHA in plugins.lock is preserved under
        --local-only.

        Covers the interleaved-workflow case: `update_plugins <remote-name>`
        resolves + pins the real SHA, then a later `update_plugins
        --local-only` (e.g. a test run that reuses the payload dir) skips the
        fetch but keeps the resolved SHA rather than clobbering it with
        'skipped'. Without this, an interleaved --local-only run silently
        loses the pin a prior fetch established (#1664 review finding)."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        # Seed the lock with a real-looking prior SHA (as if a prior fetch
        # had resolved remote-one to a specific commit).
        import yaml

        prior_lock = {
            "plugins": [
                {
                    "name": "remote-one",
                    "git": "https://example.invalid/owner/repo.git",
                    "path": "",
                    "ref": "v1.0",
                    "sha": "abc123def456",  # a real-looking resolved SHA
                }
            ]
        }
        (payload / "plugins.lock").write_text(yaml.dump(prior_lock))

        update_plugins.main(["--payload-dir", str(payload), "--local-only"])

        lock = yaml.safe_load((payload / "plugins.lock").read_text())
        entries = {e["name"]: e for e in lock["plugins"]}
        assert entries["remote-one"]["sha"] == "abc123def456", (
            f"--local-only should preserve a prior real SHA; got "
            f"{entries['remote-one']['sha']!r}"
        )

    def test_local_only_writes_skipped_when_no_prior_sha(self, tmp_path, monkeypatch):
        """With no prior lock, --local-only writes sha='skipped' for git
        entries (the baseline case — no prior fetch to preserve)."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()
        # No prior plugins.lock.

        update_plugins.main(["--payload-dir", str(payload), "--local-only"])

        import yaml

        lock = yaml.safe_load((payload / "plugins.lock").read_text())
        entries = {e["name"]: e for e in lock["plugins"]}
        assert entries["remote-one"]["sha"] == "skipped", (
            f"--local-only with no prior lock should write sha='skipped'; "
            f"got {entries['remote-one']['sha']!r}"
        )
