"""Contract tests for the dist-smoke sidecar-image provisioning (#3343).

Fresh workspaces default to ``egress_mode=interactive``, which makes the
FQDN network sidecar required and fail-closed: without a local
``klangk-network-sidecar`` image, podman resolves the unqualified default
against docker.io/quay.io and ``klangk start`` 500s — dist-smoke run
https://github.com/mcdonc/klangk/actions/runs/34172862442 was red for
exactly that reason. #3140 publishes the image to GHCR under immutable
calver-commit tags, and ``scripts/dist-smoke-test.sh`` bridges the two:
it discovers the newest tag via the anonymously readable GHCR v2 API,
pulls it, and retags it to the short name the server resolves from local
storage. A future edit that drops any half of that silently turns the
release gate red again (or worse, green while skipping the phase) — these
tests make it loud, in the grep-contract spirit of
``test_seeded_password_policy.py`` (#3341).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SMOKE_SH = _REPO_ROOT / "scripts" / "dist-smoke-test.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "dist-smoke.yml"
_SIDECAR_REPO = "mcdonc/klangk/klangk-network-sidecar"


def test_smoke_pulls_and_retags_published_sidecar():
    """The smoke must bridge GHCR's published sidecar to the short name.

    The server resolves the unqualified ``klangk-network-sidecar`` default
    against local storage, so the phase pulls the published FQ reference
    and retags it onto ``localhost/$SIDECAR_IMAGE:latest`` — dropping
    either half reproduces the #3343 500.
    """
    text = _SMOKE_SH.read_text()
    assert f'SIDECAR_NAME="{_SIDECAR_REPO}"' in text, (
        "dist-smoke no longer names the published network sidecar image "
        "repo — the CLI-phase workspace start fail-closes (#3343)"
    )
    assert 'podman pull "ghcr.io/$SIDECAR_NAME:$SIDECAR_REF"' in text, (
        "dist-smoke no longer pulls the published network sidecar image "
        "from GHCR — the CLI-phase workspace start fail-closes (#3343)"
    )
    assert (
        'SIDECAR_IMAGE="${KLANGKD_NETWORK_SIDECAR_IMAGE:-klangk-network-sidecar}"'
        in text
    ), (
        "dist-smoke no longer retags onto the server's default sidecar "
        "image short name (#3343)"
    )
    assert (
        'podman tag "ghcr.io/$SIDECAR_NAME:$SIDECAR_REF" '
        '"localhost/$SIDECAR_IMAGE:latest"' in text
    ), (
        "dist-smoke pulls the sidecar but never retags it to the short "
        "name the server resolves from local storage (#3343)"
    )


def test_sidecar_tag_discovery_filters_calver():
    """Tag discovery must keep the calver filter and version sort.

    GHCR carries only immutable tags; without the ``YYYY.MM.DD-<sha>``
    filter a plain ``max()`` could pick a differently-shaped tag (e.g. a
    ``vX.Y.Z`` release tag) and defeat the "newest publish" intent.
    """
    text = _SMOKE_SH.read_text()
    assert "newest_sidecar_tag()" in text, (
        "dist-smoke no longer discovers the newest published sidecar tag "
        "from the GHCR API (#3343)"
    )
    # The literal bash pattern as it appears in the script: calver date
    # groups with backslash-escaped dots, hyphen, short commit sha.
    calver_grep = "grep -oE '[0-9]{4}\\.[0-9]{2}\\.[0-9]{2}-[0-9a-f]+'"
    assert calver_grep in text, (
        "dist-smoke's sidecar tag discovery lost its calver-commit filter "
        "(YYYY.MM.DD-<sha>) — sorting over unfiltered tags can pick a "
        "non-calver reference (#3343)"
    )
    assert "sort -V" in text, (
        "dist-smoke's sidecar tag discovery lost its version sort (#3343)"
    )


def test_sidecar_pull_failure_is_fatal():
    """A sidecar pull failure must abort, not skip the container phase.

    The workspace-image build failure path sets SKIP_CONTAINER_SMOKE=1 and
    skips; the sidecar path must not — a skipped phase reports green while
    nothing was tested, and the start below would 500 anyway.
    """
    text = _SMOKE_SH.read_text()
    start = text.index("SIDECAR_IMAGE=")
    end = text.index("restarting klangkd in none-auth mode", start)
    block = text[start:end]
    assert "exit 1" in block, (
        "dist-smoke's sidecar provisioning no longer fails hard when the "
        "pull fails — it must be a release-blocker signal, not a skip "
        "(#3343)"
    )
    assert "SKIP_CONTAINER_SMOKE=1" not in block, (
        "dist-smoke's sidecar provisioning skips the container phase on "
        "failure — a silent skip reports green without testing the start "
        "(#3343)"
    )


def test_sidecar_provisioned_before_start():
    """The sidecar must be provisioned before the CLI starts a workspace.

    Provisioning after the start (or after the klangkd restart) leaves the
    very first start attempt failing closed.
    """
    text = _SMOKE_SH.read_text()
    assert text.index("=== pulling network sidecar image") < text.index(
        "=== CLI: start workspace ==="
    ), (
        "dist-smoke provisions the network sidecar after the workspace "
        "start that needs it (#3343)"
    )


def test_workflow_note_reports_gate_as_authoritative():
    """The workflow header must not carry the stale expected-FAIL note.

    The #1709-era "currently expected to FAIL" note predates both that fix
    and the #3343 sidecar provisioning; keeping it teaches maintainers to
    ignore a red gate (#3343).
    """
    text = _WORKFLOW.read_text()
    assert "expected to FAIL" not in text, (
        "dist-smoke.yml still tells maintainers the gate is expected to "
        "fail — with #1709 fixed and the sidecar provisioned (#3343), any "
        "failure is a release-blocker worth investigating"
    )
    assert _SIDECAR_REPO in text or "GHCR" in text, (
        "dist-smoke.yml's container-phase note should mention the GHCR "
        "sidecar provisioning so the run's image provenance is documented "
        "(#3343)"
    )
