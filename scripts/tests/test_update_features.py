"""Tests for update_features.py — local path feature support."""

import os
import shutil
import sys


# Make sure the scripts directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import update_features


class TestLinkFeature:
    def test_absolute_path(self, tmp_path):
        source = tmp_path / "my-feature"
        source.mkdir()
        (source / "extension.ts").write_text("// test")

        features_dir = tmp_path / "features"
        features_dir.mkdir()

        result = update_features.link_feature(
            {"name": "my-feature", "path": str(source)}, str(features_dir)
        )

        dest = features_dir / "my-feature"
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)
        assert result == {"name": "my-feature", "path": str(source)}

    def test_relative_path(self, tmp_path, monkeypatch):
        # features.yaml lives in tmp_path/features/
        yaml_dir = tmp_path / "features"
        yaml_dir.mkdir()
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(yaml_dir / "features.yaml")
        )

        # The actual feature source is at tmp_path/dev/my-feature
        source = tmp_path / "dev" / "my-feature"
        source.mkdir(parents=True)
        (source / "extension.ts").write_text("// test")

        features_dir = tmp_path / "installed"
        features_dir.mkdir()

        result = update_features.link_feature(
            {"name": "my-feature", "path": "../dev/my-feature"},
            str(features_dir),
        )

        dest = features_dir / "my-feature"
        assert dest.is_symlink()
        assert result is not None
        assert result["name"] == "my-feature"

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        home = tmp_path / "fakehome"
        home.mkdir()
        feature_src = home / "my-feature"
        feature_src.mkdir()

        monkeypatch.setenv("HOME", str(home))

        features_dir = tmp_path / "features"
        features_dir.mkdir()

        result = update_features.link_feature(
            {"name": "my-feature", "path": "~/my-feature"}, str(features_dir)
        )

        assert result is not None
        dest = features_dir / "my-feature"
        assert dest.is_symlink()

    def test_envvar_expansion(self, tmp_path, monkeypatch):
        source = tmp_path / "my-feature"
        source.mkdir()

        monkeypatch.setenv("MY_FEATURE_DIR", str(source))

        features_dir = tmp_path / "features"
        features_dir.mkdir()

        result = update_features.link_feature(
            {"name": "my-feature", "path": "$MY_FEATURE_DIR"},
            str(features_dir),
        )

        assert result is not None
        dest = features_dir / "my-feature"
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)

    def test_nonexistent_path_returns_none(self, tmp_path):
        features_dir = tmp_path / "features"
        features_dir.mkdir()

        result = update_features.link_feature(
            {"name": "missing", "path": str(tmp_path / "nope")},
            str(features_dir),
        )

        assert result is None
        assert not (features_dir / "missing").exists()

    def test_replaces_existing_symlink(self, tmp_path):
        old_source = tmp_path / "old"
        old_source.mkdir()
        new_source = tmp_path / "new"
        new_source.mkdir()

        features_dir = tmp_path / "features"
        features_dir.mkdir()
        dest = features_dir / "my-feature"
        os.symlink(str(old_source), str(dest))

        result = update_features.link_feature(
            {"name": "my-feature", "path": str(new_source)}, str(features_dir)
        )

        assert result is not None
        assert os.readlink(str(dest)) == str(new_source)

    def test_replaces_existing_directory(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()

        features_dir = tmp_path / "features"
        features_dir.mkdir()
        dest = features_dir / "my-feature"
        dest.mkdir()
        (dest / "old-file.txt").write_text("old")

        result = update_features.link_feature(
            {"name": "my-feature", "path": str(source)}, str(features_dir)
        )

        assert result is not None
        assert dest.is_symlink()
        assert os.readlink(str(dest)) == str(source)


class TestMainPayloadDir:
    """main() reads the checked-in features.yaml and writes to --payload-dir (#1660)."""

    def test_main_reads_repo_root_yaml_writes_payload_lock(self, tmp_path, monkeypatch):
        # Fake a repo root with features.yaml + a feature tree.
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "features.yaml").write_text(
            "features:\n  - name: demo\n    path: features/demo\n"
        )
        demo = fake_repo / "features" / "demo"
        demo.mkdir(parents=True)
        (demo / "extension.ts").write_text("// hi")

        # Point ROOT + YAML_PATH at the fake repo.
        monkeypatch.setattr(update_features, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(fake_repo / "features.yaml")
        )

        payload = tmp_path / "payload"
        payload.mkdir()
        rc = update_features.main(["--payload-dir", str(payload)])

        assert rc == 0
        # The feature is symlinked into the payload dir.
        assert (payload / "demo").is_symlink()
        # The lockfile landed in the payload dir (not next to features.yaml).
        lock = payload / "features.lock"
        assert lock.is_file()
        import yaml

        data = yaml.safe_load(lock.read_text())
        assert data["features"][0]["name"] == "demo"

    def test_main_ignores_klangk_plugins_dir_env(self, tmp_path, monkeypatch):
        # KLANGKBUILD_PLUGINS_DIR must have no effect on where the payload lands
        # (#1660 — the var is gone from every layer).
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "features.yaml").write_text("features: []\n")
        monkeypatch.setattr(update_features, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(fake_repo / "features.yaml")
        )

        bogus = tmp_path / "bogus-env-target"
        bogus.mkdir()
        monkeypatch.setenv("KLANGKBUILD_PLUGINS_DIR", str(bogus))

        payload = tmp_path / "payload"
        payload.mkdir()
        rc = update_features.main(["--payload-dir", str(payload)])

        assert rc == 0
        # Nothing was written under the env-var-named dir.
        assert not (bogus / "features.lock").exists()
        # The module no longer references the env var at all.
        assert "KLANGKBUILD_PLUGINS_DIR" not in {
            name for name, _ in update_features.__dict__.items()
        }

    def test_main_errors_when_features_yaml_missing(self, tmp_path, monkeypatch):
        # No checked-in features.yaml -> clear error, not silent template creation
        # (the old first-run bootstrap is gone; the file is source-controlled).
        empty_repo = tmp_path / "repo"
        empty_repo.mkdir()
        monkeypatch.setattr(update_features, "ROOT", str(empty_repo))
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(empty_repo / "features.yaml")
        )
        rc = update_features.main(["--payload-dir", str(tmp_path / "payload")])
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
        (fake_repo / "features.yaml").write_text(
            "features:\n  - name: solo\n    path: features/solo\n"
        )
        solo = fake_repo / "features" / "solo"
        solo.mkdir(parents=True)
        (solo / "extension.ts").write_text("// hi")
        monkeypatch.setattr(update_features, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(fake_repo / "features.yaml")
        )

        before = atexit._ncallbacks()  # type: ignore[attr-defined]
        rc = update_features.main([])
        after = atexit._ncallbacks()  # type: ignore[attr-defined]

        assert rc == 0
        assert after == before + 1, (
            "main() must register exactly one atexit cleanup for its tempdir"
        )
        # _make_temp_payload_dir used the "klangk-features-" prefix; find the
        # dir it created so we can assert contents, then clean it up (the
        # registered rmtree would fire at session end otherwise).
        import glob

        candidates = glob.glob("/tmp/klangk-features-*") + glob.glob(
            os.environ.get("TMPDIR", "/tmp") + "/klangk-features-*"
        )
        assert candidates, "expected a klangk-features-* tempdir to exist"
        payload_path = max(candidates, key=os.path.getmtime)
        assert os.path.isfile(os.path.join(payload_path, "features.lock"))
        # Reap now so the test is tidy; unregister so atexit doesn't touch it.
        shutil.rmtree(payload_path, ignore_errors=True)


class TestLocalOnlyFlag:
    """--local-only skips git-sourced features without hitting the network (#1664).

    Tests use this to verify the local-feature contract without cloning remote
    repos. Git entries land in features.lock with sha: 'skipped' so the lock
    shape stays consistent (every declared feature appears).
    """

    def _setup_repo(self, tmp_path, monkeypatch):
        """A repo with one local + one git feature declared."""
        fake_repo = tmp_path / "repo"
        fake_repo.mkdir()
        (fake_repo / "features.yaml").write_text(
            "features:\n"
            "  - name: local-one\n"
            "    path: features/local-one\n"
            "  - name: remote-one\n"
            "    git: https://example.invalid/owner/repo.git\n"
            "    ref: v1.0\n"
        )
        local = fake_repo / "features" / "local-one"
        local.mkdir(parents=True)
        (local / "extension.ts").write_text("// hi")
        monkeypatch.setattr(update_features, "ROOT", str(fake_repo))
        monkeypatch.setattr(
            update_features, "YAML_PATH", str(fake_repo / "features.yaml")
        )
        return fake_repo

    def test_skips_git_entries(self, tmp_path, monkeypatch):
        """Git entries are skipped (not fetched) under --local-only."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        # Would normally try to clone example.invalid; --local-only skips it.
        rc = update_features.main(["--payload-dir", str(payload), "--local-only"])
        assert rc == 0

        # Only the local feature is materialized as a directory.
        materialized = {p for p in os.listdir(payload) if (payload / p).is_dir()}
        assert materialized == {"local-one"}, (
            f"--local-only should skip remote-one; materialized={materialized}"
        )

    def test_git_entries_recorded_as_skipped_in_lock(self, tmp_path, monkeypatch):
        """Skipped git entries still appear in features.lock with sha='skipped'.

        The lock shape stays consistent (every declared feature appears) so a
        downstream consumer doesn't see features vanishing from the lock when
        the build runs --local-only."""
        self._setup_repo(tmp_path, monkeypatch)
        payload = tmp_path / "payload"
        payload.mkdir()

        update_features.main(["--payload-dir", str(payload), "--local-only"])

        import yaml

        lock = yaml.safe_load((payload / "features.lock").read_text())
        entries = {e["name"]: e for e in lock["features"]}
        assert set(entries) == {"local-one", "remote-one"}, (
            f"lock should list both features; got {sorted(entries)}"
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

        # No --local-only: the bogus git URL is attempted. fetch_feature prints
        # an ERROR and returns None; the feature is dropped from the lock
        # (the silent-fallback-to-default-branch guard from #1660). Use a
        # file:// URL so the test doesn't hit DNS (no network access in the
        # scripts test suite).
        fake_repo = tmp_path / "repo"
        # Override the git URL to a file:// path that doesn't exist.
        (fake_repo / "features.yaml").write_text(
            "features:\n"
            "  - name: local-one\n"
            "    path: features/local-one\n"
            "  - name: remote-one\n"
            "    git: file:///nonexistent/path/repo.git\n"
            "    ref: v1.0\n"
        )

        rc = update_features.main(["--payload-dir", str(payload)])
        assert rc == 0  # main() doesn't exit nonzero on a failed fetch

        import yaml

        lock = yaml.safe_load((payload / "features.lock").read_text())
        names = {e["name"] for e in lock["features"]}
        # remote-one was attempted, failed, and dropped — only local-one lands.
        assert names == {"local-one"}, (
            f"failed-fetch feature should be dropped; lock has {sorted(names)}"
        )

    def test_local_only_preserves_prior_real_sha(self, tmp_path, monkeypatch):
        """A pre-existing real SHA in features.lock is preserved under
        --local-only.

        Covers the interleaved-workflow case: `update_features <remote-name>`
        resolves + pins the real SHA, then a later `update_features
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
            "features": [
                {
                    "name": "remote-one",
                    "git": "https://example.invalid/owner/repo.git",
                    "path": "",
                    "ref": "v1.0",
                    "sha": "abc123def456",  # a real-looking resolved SHA
                }
            ]
        }
        (payload / "features.lock").write_text(yaml.dump(prior_lock))

        update_features.main(["--payload-dir", str(payload), "--local-only"])

        lock = yaml.safe_load((payload / "features.lock").read_text())
        entries = {e["name"]: e for e in lock["features"]}
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
        # No prior features.lock.

        update_features.main(["--payload-dir", str(payload), "--local-only"])

        import yaml

        lock = yaml.safe_load((payload / "features.lock").read_text())
        entries = {e["name"]: e for e in lock["features"]}
        assert entries["remote-one"]["sha"] == "skipped", (
            f"--local-only with no prior lock should write sha='skipped'; "
            f"got {entries['remote-one']['sha']!r}"
        )
