"""Unit tests for klangk.version's resolution chain (#3517).

The chain: ``version_file`` setting → wheel-packaged copy → dev block,
with the #3329 tolerance (absent, unreadable, or wrong-shaped input
falls through to the next source instead of raising).
"""

from __future__ import annotations

import json

from klangk import version


class _FakeResource:
    """Stands in for an importlib.resources traversable."""

    def __init__(self, text: str):
        self._text = text

    def joinpath(self, name: str) -> "_FakeResource":
        return self

    def read_text(self) -> str:
        return self._text


class TestReadVersionFile:
    def test_reads_json_object(self, tmp_path):
        path = tmp_path / "version.json"
        path.write_text(json.dumps({"version": "1.2.3", "commit": "abc"}))
        assert version.read_version_file(str(path)) == {
            "version": "1.2.3",
            "commit": "abc",
        }

    def test_missing_file_returns_none(self, tmp_path):
        assert version.read_version_file(str(tmp_path / "absent.json")) is None

    def test_unparseable_file_returns_none(self, tmp_path):
        path = tmp_path / "version.json"
        path.write_text("{not json")
        assert version.read_version_file(str(path)) is None

    def test_non_dict_file_returns_none(self, tmp_path):
        path = tmp_path / "version.json"
        path.write_text('["1.2.3"]')
        assert version.read_version_file(str(path)) is None


class TestPackagedVersion:
    def test_absent_in_editable_install_returns_none(self):
        # The devenv runs an editable install and klangk/version.json is
        # never a source file (the hatch hook generates it only into
        # non-editable wheels, #3517) — this pins the fall-through
        # branch against the real resources machinery.
        assert version.packaged_version() is None

    def test_returns_parsed_object(self, monkeypatch):
        payload = json.dumps(
            {"version": "9.9.9", "variant": "acme", "commit": "def0123"}
        )
        monkeypatch.setattr(
            version, "files", lambda pkg: _FakeResource(payload)
        )
        assert version.packaged_version() == {
            "version": "9.9.9",
            "variant": "acme",
            "commit": "def0123",
        }

    def test_corrupt_packaged_file_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            version, "files", lambda pkg: _FakeResource("{not json")
        )
        assert version.packaged_version() is None

    def test_non_dict_packaged_file_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            version, "files", lambda pkg: _FakeResource('["9.9.9"]')
        )
        assert version.packaged_version() is None


class TestVersionInfo:
    def _settings(self, version_file):
        import types

        return types.SimpleNamespace(version_file=version_file)

    def test_configured_file_wins(self, tmp_path, monkeypatch):
        path = tmp_path / "version.json"
        path.write_text(json.dumps({"version": "from-file"}))
        monkeypatch.setattr(
            version, "packaged_version", lambda: {"version": "from-wheel"}
        )
        assert version.version_info(self._settings(str(path))) == {
            "version": "from-file"
        }

    def test_unreadable_configured_file_falls_to_packaged(
        self, tmp_path, monkeypatch
    ):
        # An operator config pointing at a file that is gone still gets
        # the wheel's copy (#3517) — most-information-wins.
        monkeypatch.setattr(
            version,
            "packaged_version",
            lambda: {"version": "from-wheel"},
        )
        absent = str(tmp_path / "absent.json")
        assert version.version_info(self._settings(absent)) == {
            "version": "from-wheel"
        }

    def test_unset_setting_falls_to_packaged(self, monkeypatch):
        monkeypatch.setattr(
            version,
            "packaged_version",
            lambda: {"version": "from-wheel", "commit": "abc"},
        )
        assert version.version_info(self._settings(None)) == {
            "version": "from-wheel",
            "commit": "abc",
        }

    def test_no_source_reports_the_dev_block(self, monkeypatch):
        monkeypatch.setattr(version, "packaged_version", lambda: None)
        info = version.version_info(self._settings(None))
        assert info == version.DEV_BLOCK

    def test_dev_block_result_is_a_copy(self, monkeypatch):
        # The endpoint adds "features" to whatever version_info returns
        # — mutating the result must not poison later calls (#3517).
        monkeypatch.setattr(version, "packaged_version", lambda: None)
        first = version.version_info(self._settings(None))
        first["version"] = "mutated"
        first["features"] = ["x"]
        again = version.version_info(self._settings(None))
        assert again["version"] == "dev"
        assert "features" not in again
