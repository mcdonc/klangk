"""Contract tests for pinned third-party artifacts at image-build time (#2063).

Every artifact fetched from the network during an image build must be
verified against a hash pinned in the Dockerfile (or pulled by immutable
digest), so a compromised registry/CDN or a MITM cannot swap content
undetected. These are grep-style contract tests in the spirit of
``test_build_script_remote_guard.py``: they assert the *verification
mechanism* is present in the real Dockerfiles, so an edit that silently
removes a pin is loud.

What is pinned where (see docs/development/building-images.md for the
rotation procedures):

- ``src/containers/workspace/Dockerfile``
    - workspace base image: ``@sha256:`` digest (bumped by the auto-PR from
      .github/workflows/image-workspace-base.yml)
    - pi agent npm tarball: SHA-512 (registry ``dist.integrity``)
    - uv release tarball: per-arch SHA-256 (amd64/arm64)
    - process-compose release tarball: per-arch SHA-256 (amd64/arm64)
- ``src/containers/workspace/Dockerfile.base``
    - debian:trixie-slim: digest (pre-existing, #2432)
    - nodesource + github-cli repo keys: SHA-256 of the fetched key file
- ``src/containers/host/Dockerfile``
    - python:3.13-slim: digest
    - Caddy Cloudsmith repo key: SHA-256 of the fetched key file
- ``src/containers/network/Dockerfile`` — alpine:3.21: digest
- ``src/containers/{workspace,host}/Dockerfile.fips`` and
  ``src/containers/nix-seed/Dockerfile`` — debian:trixie-slim builders
  share Dockerfile.base's digest pin.

Caddy's apt sources list is written inline (not fetched), so the repo key is
the only network-sourced trust input for that repo; apt's own GPG
verification then covers the package indexes and debs.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_WORKSPACE_DF = _ROOT / "src/containers/workspace/Dockerfile"
_WORKSPACE_BASE_DF = _ROOT / "src/containers/workspace/Dockerfile.base"
_HOST_DF = _ROOT / "src/containers/host/Dockerfile"
_NETWORK_DF = _ROOT / "src/containers/network/Dockerfile"

# Every Dockerfile that participates in image builds. FROM lines must be
# digest-pinned, an ARG reference, or a local build-stage alias — never a
# mutable registry tag.
_IMAGE_DOCKERFILES = [
    _WORKSPACE_DF,
    _WORKSPACE_BASE_DF,
    _WORKSPACE_DF.with_name("Dockerfile.fips"),
    _HOST_DF,
    _HOST_DF.with_name("Dockerfile.fips"),
    _NETWORK_DF,
    _ROOT / "src/containers/nix-seed/Dockerfile",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")


def _arg(df: Path, name: str) -> str:
    """Value of a top-level ``ARG name=value`` line (no default → '')."""
    m = re.search(rf"^ARG {name}=(\S+)", df.read_text(), re.MULTILINE)
    return m.group(1) if m else ""


# ── Base/host images: immutable digest references ───────────────────────────


class TestFromLinesPinnedByDigest:
    def test_every_image_dockerfile_from_line_is_digest_arg_or_alias(self):
        for df in _IMAGE_DOCKERFILES:
            text = df.read_text()
            froms = re.findall(r"(?im)^FROM\s+(\S+)", text)
            assert froms, f"{df} has no FROM lines — test is stale"
            # Stage names defined in this file (``FROM … AS name``) may be
            # used as later FROM targets without a digest.
            stage_names = {
                m.lower() for m in re.findall(r"(?im)^FROM\s+\S+\s+AS\s+(\S+)", text)
            }
            for ref in froms:
                if ref.startswith("$"):
                    continue  # ARG reference (value pinned elsewhere)
                if ref.lower() in stage_names:
                    continue  # local build-stage alias
                assert "@sha256:" in ref, (
                    f"{df}: FROM {ref} is not pinned by digest (#2063) — "
                    f"mutable-tag base images let the registry serve "
                    f"different content for the same reference"
                )

    def test_workspace_base_image_arg_is_digest(self):
        ref = _arg(_WORKSPACE_DF, "WORKSPACE_BASE_IMAGE")
        assert re.match(
            r"^ghcr\.io/mcdonc/klangk/klangk-workspace-base@sha256:[0-9a-f]{64}$",
            ref,
        ), f"WORKSPACE_BASE_IMAGE must be a full repo@digest ref, got {ref!r}"

    def test_fips_and_nix_seed_share_dockerfile_base_digest(self):
        pinned = (
            "debian:trixie-slim@"
            "sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
        )
        assert pinned in _WORKSPACE_BASE_DF.read_text(), (
            "Dockerfile.base's debian digest pin changed — update this test "
            "AND the three aligned builders (workspace/Dockerfile.fips, "
            "host/Dockerfile.fips, nix-seed/Dockerfile) together"
        )
        aligned_builders = [
            _ROOT / "src/containers/workspace/Dockerfile.fips",
            _ROOT / "src/containers/host/Dockerfile.fips",
            _ROOT / "src/containers/nix-seed/Dockerfile",
        ]
        for df in aligned_builders:
            assert pinned in df.read_text(), (
                f"{df} must pin the same debian:trixie-slim digest as Dockerfile.base"
            )

    def test_base_image_workflow_pins_digest_in_auto_pr(self):
        wf = (_ROOT / ".github/workflows/image-workspace-base.yml").read_text()
        assert "imagetools inspect" in wf, (
            "the base-image workflow must resolve the pushed manifest digest"
        )
        # The json form is load-bearing: buildx special-cases plain
        # '{{.Manifest…}}' --format values into printing the full table,
        # which would corrupt the DIGEST capture (found in #2788 review).
        assert "{{json .Manifest.Digest}}" in wf, (
            "the digest extraction must use the json form — plain "
            "'{{.Manifest.Digest}}' makes buildx print the whole table"
        )
        assert 'echo "image=$REPO@$DIGEST"' in wf, (
            "the auto-PR must rewrite WORKSPACE_BASE_IMAGE to a repo@digest "
            "reference, not a mutable tag (#2063)"
        )

    def test_pull_base_image_pulls_the_pinned_reference(self):
        script = (_ROOT / "scripts/pull-base-image.sh").read_text()
        code_lines = [
            ln for ln in script.splitlines() if not ln.lstrip().startswith("#")
        ]
        assert any("WORKSPACE_BASE_IMAGE" in ln for ln in code_lines), (
            "pull-base-image.sh must pull the reference pinned in the "
            "workspace Dockerfile"
        )
        assert not any(":latest" in ln for ln in code_lines), (
            "pull-base-image.sh must not pull a mutable :latest tag (#2063)"
        )


# ── Workspace image: verified tarballs (pi agent, uv, process-compose) ───────


class TestWorkspaceTarballPins:
    def test_pi_agent_tarball_is_sha512_verified(self):
        text = _WORKSPACE_DF.read_text()
        assert (
            "registry.npmjs.org/@earendil-works/pi-coding-agent/-/"
            "pi-coding-agent-${PI_AGENT_VERSION}.tgz" in text
        ), "pi agent must be installed from the registry tarball URL"
        assert re.search(
            r"\$\{PI_AGENT_SHA512\}\s+/tmp/pi-agent\.tgz.*\| sha512sum -c -",
            text,
        ), "pi agent tarball must be verified (sha512sum -c) before install"
        assert _HEX128.match(_arg(_WORKSPACE_DF, "PI_AGENT_SHA512")), (
            "PI_AGENT_SHA512 must be a 128-hex-char sha512"
        )
        assert "npm install -g /tmp/pi-agent.tgz" in text, (
            "pi agent must be installed from the verified local tarball, not "
            "a registry-resolved version string"
        )

    def test_uv_is_per_arch_sha256_verified(self):
        text = _WORKSPACE_DF.read_text()
        assert "astral.sh" not in text, (
            "uv must not be installed via the astral.sh piped installer (#2063)"
        )
        assert re.search(r"\$\{sha\}\s+/tmp/uv\.tar\.gz.*\| sha256sum -c -", text), (
            "uv tarball must be verified (sha256sum -c) before extraction"
        )
        for arch in ("AMD64", "ARM64"):
            assert _HEX64.match(_arg(_WORKSPACE_DF, f"UV_SHA256_{arch}")), (
                f"UV_SHA256_{arch} must be a 64-hex-char sha256"
            )
        # Both arches must map to their own pin inside the build.
        assert 'sha="$UV_SHA256_AMD64"' in text and 'sha="$UV_SHA256_ARM64"' in text

    def test_process_compose_is_per_arch_sha256_verified(self):
        text = _WORKSPACE_DF.read_text()
        assert re.search(
            r"\$\{sha\}\s+/tmp/process-compose\.tar\.gz.*\| sha256sum -c -", text
        ), "process-compose tarball must be verified (sha256sum -c) before tar"
        for arch in ("AMD64", "ARM64"):
            assert _HEX64.match(
                _arg(_WORKSPACE_DF, f"PROCESS_COMPOSE_SHA256_{arch}")
            ), f"PROCESS_COMPOSE_SHA256_{arch} must be a 64-hex-char sha256"

    def test_no_unverified_curl_pipes(self):
        """No `curl ... | sh` / `curl ... | tar` piping in the image
        Dockerfiles whose installs are fully pinned (workspace, host, base) —
        download-to-file, verify, then act. The regex `curl[^|]*\|` matches
        any line with `curl` followed later by a pipe (a `\S*`-anchored form
        misses the space between flag and URL, which is how the original
        `curl | sh` lines would slip past). nix-seed is excluded: its piped
        nix installer is a documented accepted residual (see
        docs/development/building-images.md, "Accepted residuals")."""
        for df in (_WORKSPACE_DF, _HOST_DF, _WORKSPACE_BASE_DF):
            text = df.read_text()
            code_lines = [
                ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
            ]
            for line in code_lines:
                assert not re.search(r"curl[^|]*\|", line), (
                    f"piped curl in {df.name} (unverified network input, "
                    f"#2063): {line.strip()}"
                )


# ── Apt repo keys: verified before entering a keyring ───────────────────────


class TestAptRepoKeyPins:
    def test_caddy_key_is_verified_and_sources_written_inline(self):
        text = _HOST_DF.read_text()
        assert re.search(
            r"\$\{CADDY_REPO_KEY_SHA256\}\s+/tmp/caddy-stable\.key.*"
            r"\| sha256sum -c -",
            text,
        ), "Caddy repo key must be sha256-verified before dearmor (#2063)"
        assert _HEX64.match(_arg(_HOST_DF, "CADDY_REPO_KEY_SHA256")), (
            "CADDY_REPO_KEY_SHA256 must be a 64-hex-char sha256"
        )
        # The sources list is written inline, not fetched over the network.
        assert (
            "echo 'deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg]"
            " https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main'"
            in text
        ), "Caddy apt sources list must be written inline (#2063)"
        assert "debian.deb.txt" not in text, (
            "Caddy sources list must not be fetched from the network (#2063)"
        )

    def test_nodesource_and_githubcli_keys_are_verified(self):
        text = _WORKSPACE_BASE_DF.read_text()
        assert re.search(
            r"\$\{NODESOURCE_KEY_SHA256\}\s+/tmp/nodesource-repo\.gpg\.key.*"
            r"\| sha256sum -c -",
            text,
        ), "NodeSource repo key must be sha256-verified before dearmor (#2063)"
        assert _HEX64.match(_arg(_WORKSPACE_BASE_DF, "NODESOURCE_KEY_SHA256"))
        assert re.search(
            r"\$\{GITHUBCLI_KEYRING_SHA256\}\s+/tmp/githubcli-archive-keyring\.gpg.*"
            r"\| sha256sum -c -",
            text,
        ), "GitHub CLI keyring must be sha256-verified before use (#2063)"
        assert _HEX64.match(_arg(_WORKSPACE_BASE_DF, "GITHUBCLI_KEYRING_SHA256"))

    def test_no_unverified_key_fetches_in_base_or_host(self):
        """Every gpg key fetch in the base/host images lands in a file that a
        pinned-hash check reads before the key enters a keyring."""
        for df, needles in (
            (
                _WORKSPACE_BASE_DF,
                ("nodesource-repo.gpg.key", "githubcli-archive-keyring.gpg"),
            ),
            (_HOST_DF, ("caddy/stable/gpg.key",)),
        ):
            text = df.read_text()
            for needle in needles:
                assert "-o /tmp/" in text and needle in text, (
                    f"{df}: {needle} must be downloaded to a file for hash "
                    f"verification, not piped straight into a keyring (#2063)"
                )
