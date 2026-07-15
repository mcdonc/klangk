"""Tests for the _instance_id CLI front-end."""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest


def _run_shim(
    data_dir: Path, state_dir: Path | None = None
) -> subprocess.CompletedProcess:
    """Run the klangk-instance-id console script against ``data_dir``."""
    state = state_dir if state_dir is not None else data_dir
    return subprocess.run(
        [sys.executable, "-m", "klangk_backend._instance_id"],
        capture_output=True,
        text=True,
        env={
            "KLANGK_DATA_DIR": str(data_dir),
            "KLANGK_STATE_DIR": str(state),
            "PATH": "/dev/null",
        },
    )


class TestMain:
    def test_reads_file_klangkd_wrote(self, db, capsys, monkeypatch):
        """When the file exists, the shim prints its contents without a DB open."""
        from klangk_backend.model import instance

        path = instance.instance_id_path()
        assert path.exists()  # db fixture's resolve_instance_id wrote it
        written = path.read_text().strip()

        monkeypatch.setattr("sys.argv", ["klangk-instance-id"])
        from klangk_backend._instance_id import main

        main()
        out = capsys.readouterr().out
        assert out == written
        # It is a valid UUID-4 (klangkd generated it).
        assert uuid.UUID(out).version == 4

    def test_absent_file_exits_nonzero(self, tmp_path, capsys, monkeypatch):
        """A missing file (klangkd hasn't booted) is an error, not a generation."""
        # Point the shim's data_dir at an empty dir; no file written.
        monkeypatch.setattr("sys.argv", ["klangk-instance-id"])
        monkeypatch.setattr(
            "klangk_backend._instance_id.instance_id_path",
            lambda: tmp_path / "instance-id",
        )
        from klangk_backend._instance_id import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "does not exist" in err
        # Crucially: no file was created by the read-only shim.
        assert not (tmp_path / "instance-id").exists()

    def test_wrong_arg_count_exits(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["klangk-instance-id", "extra"])
        from klangk_backend._instance_id import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert "usage" in capsys.readouterr().err.lower()

    def test_subprocess_does_not_open_db(self, db, tmp_path):
        """End-to-end: the console script reads the file in a fresh process.

        ``PATH=/dev/null`` keeps it from finding a sqlite shell, but more
        to the point: it resolves the same data_dir from the environment
        and reads the file klangkd wrote, never touching the DB.
        """
        from klangk_backend.model import instance

        data_dir = instance.instance_id_path().parent
        result = _run_shim(data_dir)
        assert result.returncode == 0, result.stderr
        assert uuid.UUID(result.stdout.strip()).version == 4

    def test_subprocess_absent_file(self, tmp_path):
        """In a real subprocess, a missing file exits 1 with no stdout."""
        result = _run_shim(tmp_path)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "does not exist" in result.stderr
