"""Tests for the _instance_id CLI front-end."""

import uuid

import pytest

from klangk_backend._instance_id import main


class TestMain:
    def test_prints_uuid(self, db, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["klangk-instance-id"])
        main()
        out = capsys.readouterr().out
        parsed = uuid.UUID(out)
        assert parsed.version == 4

    def test_idempotent(self, db, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["klangk-instance-id"])
        main()
        first = capsys.readouterr().out
        main()
        second = capsys.readouterr().out
        assert first == second

    def test_wrong_arg_count_exits(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["klangk-instance-id", "extra"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert "usage" in capsys.readouterr().err.lower()
