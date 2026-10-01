"""Host-container entrypoint contract tests (#3496).

The host entrypoint loads the embedded workspace and network-sidecar image
tars into the nested podman store. The load is **unconditional**: the store
is a persistent bind mount, so a load guarded by ``podman image exists``
keeps the tag an earlier klangk version left behind, and every new workspace
runs that old workspace image after a host upgrade. These tests run the real
entrypoint against a stub ``podman`` and assert both loads happen on every
start — including a restart where the images already exist.
"""

import os
import subprocess
import textwrap

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRYPOINT = os.path.join(REPO_ROOT, "src", "containers", "host", "entrypoint.sh")
WORKSPACE_LOAD = "load -i {home}/workspace.tar"
SIDECAR_LOAD = "load -i {home}/network-sidecar.tar"


def write_executable(path, body):
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host-container home plus stub ``podman``/``supervisord``.

    The stub podman records every invocation to a log file and answers
    success for anything — including ``image exists`` — so the entrypoint's
    load decision depends only on the entrypoint itself.
    """
    home = tmp_path / "home"
    (home / "etc").mkdir(parents=True)
    (home / "etc" / "klangkd.yaml").write_text(
        "image_name: klangk-workspace\nnetwork_sidecar_image: klangk-network-sidecar\n"
    )
    (home / "etc" / "supervisord.conf").write_text("# stub\n")
    (home / "workspace.tar").write_bytes(b"not-a-real-tar")
    (home / "network-sidecar.tar").write_bytes(b"not-a-real-tar")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    podman_log = tmp_path / "podman.log"
    write_executable(
        stub_bin / "podman",
        f"""\
        #!/bin/sh
        printf '%s\\n' "$*" >> "{podman_log}"
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
    """Run the real entrypoint with HOME pointed at the fake home."""
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        ["bash", ENTRYPOINT, "start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def podman_log_lines(home):
    log = home.parent / "podman.log"
    return log.read_text().splitlines() if log.exists() else []


def test_bundled_images_load_on_first_start(host):
    run_entrypoint(host)
    lines = podman_log_lines(host)
    assert WORKSPACE_LOAD.format(home=host) in lines
    assert SIDECAR_LOAD.format(home=host) in lines


def test_bundled_images_reload_on_restart(host):
    """The regression from #3496: a restart must refresh the tags, not skip
    the loads because images with those names already exist."""
    run_entrypoint(host)
    run_entrypoint(host)
    lines = podman_log_lines(host)
    assert lines.count(WORKSPACE_LOAD.format(home=host)) == 2
    assert lines.count(SIDECAR_LOAD.format(home=host)) == 2


def test_missing_tars_load_nothing(host):
    """A host image built without embedded tars starts cleanly and loads
    nothing (dev builds, ``build-host-image.sh`` partial flows)."""
    (host / "workspace.tar").unlink()
    (host / "network-sidecar.tar").unlink()
    run_entrypoint(host)
    assert podman_log_lines(host) == []
