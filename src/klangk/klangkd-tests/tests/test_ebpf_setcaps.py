"""klangk-ebpf-setcaps tests (#2520).

The helper's job is resolution + invocation; setcap itself is only ever
executed via mocks (actually applying caps needs root, which the test
environment never has).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from klangk import ebpf_setcaps
from klangk.settings import KlangkSettings


def test_resolve_default(monkeypatch):
    monkeypatch.delenv("KLANGKD_PROCESS_LEDGER_WATCHER", raising=False)
    p = ebpf_setcaps.resolve_binary(None, None)
    assert p == Path(ebpf_setcaps.__file__).parent / "procleddy-ebpf"


def test_resolve_explicit_path_wins(monkeypatch):
    monkeypatch.setenv("KLANGKD_PROCESS_LEDGER_WATCHER", "/from/env")
    p = ebpf_setcaps.resolve_binary("/explicit", None)
    assert p == Path("/explicit")


def test_resolve_watcher_setting(monkeypatch):
    import os

    monkeypatch.setenv("KLANGKD_PROCESS_LEDGER_WATCHER", "/from/env")
    settings = KlangkSettings(os.environ, config_file="none")
    assert str(settings.process_ledger_watcher) == "/from/env"
    p = ebpf_setcaps.resolve_binary(None, None)
    assert p == Path("/from/env")


def test_main_missing_binary(capsys):
    rc = ebpf_setcaps.main(["--path", "/nonexistent/procleddy-ebpf"])
    assert rc == 1
    assert "not found at /nonexistent" in capsys.readouterr().err


def test_main_no_setcap(tmp_path, capsys):
    binary = tmp_path / "procleddy-ebpf"
    binary.write_text("#!/bin/sh\n")
    with patch.object(ebpf_setcaps.shutil, "which", return_value=None):
        rc = ebpf_setcaps.main(["--path", str(binary)])
    assert rc == 1
    assert "setcap not found" in capsys.readouterr().err


def _fake_run_factory(out="", err="", rc=0):
    def _fake_run(cmd, capture_output=True, text=True):  # noqa: ARG001
        return SimpleNamespace(
            returncode=rc, stdout=out, stderr=err
        )

    return _fake_run


def test_main_setcap_failure(tmp_path, capsys):
    binary = tmp_path / "procleddy-ebpf"
    binary.write_text("#!/bin/sh\n")
    with (
        patch.object(
            ebpf_setcaps.shutil,
            "which",
            side_effect=lambda n: f"/usr/sbin/{n}",
        ),
        patch.object(
            subprocess,
            "run",
            _fake_run_factory(err="Operation not permitted", rc=1),
        ),
    ):
        rc = ebpf_setcaps.main(["--path", str(binary)])
    assert rc == 1
    assert "Operation not permitted" in capsys.readouterr().err


def test_main_success_with_getcap(tmp_path, capsys):
    binary = tmp_path / "procleddy-ebpf"
    binary.write_text("#!/bin/sh\n")
    with (
        patch.object(
            ebpf_setcaps.shutil,
            "which",
            side_effect=lambda n: f"/usr/sbin/{n}",
        ),
        patch.object(
            subprocess,
            "run",
            _fake_run_factory(
                out=f"{binary} cap_bpf,cap_perfmon=ep"
            ),
        ),
    ):
        rc = ebpf_setcaps.main(["--path", str(binary)])
    assert rc == 0
    assert "cap_bpf,cap_perfmon=ep" in capsys.readouterr().out


def test_main_success_without_getcap(tmp_path, capsys):
    binary = tmp_path / "procleddy-ebpf"
    binary.write_text("#!/bin/sh\n")
    with (
        patch.object(
            ebpf_setcaps.shutil,
            "which",
            side_effect=lambda n: "/usr/sbin/setcap" if n == "setcap" else None,
        ),
        patch.object(
            subprocess,
            "run",
            _fake_run_factory(),
        ),
    ):
        rc = ebpf_setcaps.main(["--path", str(binary)])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"applied {ebpf_setcaps.CAPS} to {binary}" in out


def test_cli_help():
    with pytest.raises(SystemExit) as exc:
        ebpf_setcaps.main(["--help"])
    assert exc.value.code == 0
