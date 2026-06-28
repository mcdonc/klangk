"""Tests for the _resolve_secret CLI front-end.

The prefix logic itself lives in klangk_backend.util (covered by
test_util.py); here we only verify the argv/stdout wiring of the console
script entry point (klangk_backend._resolve_secret:main).
"""

import pytest

from klangk_backend._resolve_secret import main


def test_main_prints_plain_value(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["klangk-resolve-secret", "plain-value"])
    main()
    assert capsys.readouterr().out == "plain-value"


def test_main_resolves_cmd_prefix(capsys, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["klangk-resolve-secret", "cmd:echo hi | tr a-z A-Z"]
    )
    main()
    assert capsys.readouterr().out == "HI"


def test_main_resolves_file_prefix(capsys, monkeypatch, tmp_path):
    f = tmp_path / "secret"
    f.write_text("from-file\n")
    monkeypatch.setattr("sys.argv", ["klangk-resolve-secret", f"file:{f}"])
    main()
    assert capsys.readouterr().out == "from-file"


def test_main_wrong_arg_count_exits(capsys, monkeypatch):
    # No value argument -> usage message + exit code 2.
    monkeypatch.setattr("sys.argv", ["klangk-resolve-secret"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
