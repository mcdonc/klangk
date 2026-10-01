"""Host-container entrypoint contract tests (#3496).

The host entrypoint loads the embedded workspace and network-sidecar image
tars into the nested podman store. The load is **unconditional**: the store
is a persistent bind mount, so a load guarded by ``podman image exists``
keeps the tag an earlier klangk version left behind, and every new workspace
runs that old workspace image after a host upgrade. A failed load is loud but
non-fatal: the host still boots on the previously loaded tag. These tests run
the real entrypoint against a stub ``podman`` and pin both behaviors.
"""

import os
import subprocess
import textwrap

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRYPOINT = os.path.join(REPO_ROOT, "src", "containers", "host", "entrypoint.sh")
PRUNE_LINE = "image prune -f"


def write_executable(path, body):
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host-container home plus stub ``podman``/``supervisord``.

    The stub podman records every invocation to a log file and answers
    success for anything — including ``image exists`` — so the entrypoint's
    load decision depends only on the entrypoint itself. Creating the
    ``fail-load`` marker makes the stub fail every ``load`` invocation.
    """
    home = tmp_path / "home"
    (home / "etc").mkdir(parents=True)
    (home / "etc" / "supervisord.conf").write_text("# stub\n")
    (home / "workspace.tar").write_bytes(b"not-a-real-tar")
    (home / "network-sidecar.tar").write_bytes(b"not-a-real-tar")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    write_executable(
        stub_bin / "podman",
        f"""\
        #!/bin/sh
        printf '%s\\n' "$*" >> "{tmp_path / "podman.log"}"
        if [ -f "{tmp_path / "fail-load"}" ] && [ "$1" = "load" ]; then
          exit 1
        fi
        exit 0
        """,
    )
    write_executable(
        stub_bin / "supervisord",
        """\
        #!/bin/sh
        exit 0
        """,
    )
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")
    return home


def run_entrypoint(home):
    """Run the real entrypoint with HOME pointed at the fake home.

    The entrypoint must exit 0 in every scenario tested here — including a
    failing `podman load`, which is loud but non-fatal.
    """
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        ["bash", ENTRYPOINT, "start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def podman_log_lines(home):
    log = home.parent / "podman.log"
    return log.read_text().splitlines() if log.exists() else []


def workspace_load(home):
    return f"load -i {home}/workspace.tar"


def sidecar_load(home):
    return f"load -i {home}/network-sidecar.tar"


def test_bundled_images_load_on_first_start(host):
    run_entrypoint(host)
    lines = podman_log_lines(host)
    assert workspace_load(host) in lines
    assert sidecar_load(host) in lines
    assert PRUNE_LINE in lines


def test_bundled_images_reload_on_restart(host):
    """The regression from #3496: a restart must refresh the tags, not skip
    the loads because images with those names already exist."""
    run_entrypoint(host)
    run_entrypoint(host)
    lines = podman_log_lines(host)
    assert lines.count(workspace_load(host)) == 2
    assert lines.count(sidecar_load(host)) == 2
    assert lines.count(PRUNE_LINE) == 2


def test_missing_tars_load_nothing(host):
    """A host image built without embedded tars starts cleanly, loads
    nothing, and still runs the (best-effort) prune."""
    (host / "workspace.tar").unlink()
    (host / "network-sidecar.tar").unlink()
    run_entrypoint(host)
    assert podman_log_lines(host) == [PRUNE_LINE]


def test_missing_sidecar_tar_loads_workspace_only(host):
    (host / "network-sidecar.tar").unlink()
    run_entrypoint(host)
    lines = podman_log_lines(host)
    assert workspace_load(host) in lines
    assert sidecar_load(host) not in lines


def test_load_failure_is_not_fatal(host):
    """A failing `podman load` (disk full, store locked, corrupt tar) logs a
    warning and the host still boots on the previously loaded images."""
    (host.parent / "fail-load").write_text("")
    proc = run_entrypoint(host)
    assert proc.stderr.count("Warning: podman load") == 2
    lines = podman_log_lines(host)
    assert lines.count(workspace_load(host)) == 1
    assert lines.count(sidecar_load(host)) == 1
