"""FIPS mode enforcement: generic activation probes (#2570, #2591).

``KLANGKD_FIPS_MODE`` makes klangkd verify — and refuse to run without —
an actively-enforcing OpenSSL FIPS provider:

- at every workspace-container start (the single
  :meth:`~klangk.container.registry.ContainerRegistry.create_and_start`
  choke point), probing inside the container and failing closed — the
  container is removed and the start raises;
- at backend startup, probing klangkd's own process (its password
  hashing and JWT signing go through this OpenSSL) and logging the
  result for audit — a warning, not a boot abort, since klangkd may
  legitimately run on a control host whose OpenSSL is not the FIPS
  variant while every workspace it launches is.

  **Containerized backend exception (#2628):** when klangkd itself runs
  inside a container (the docker host-container deployment — we ship
  that image, so there is no "the control host is the operator's
  problem" excuse), a failed process probe **aborts the boot** instead
  of warning. Containerization is detected via the standard marker
  files (``/.dockerenv``, ``/run/.containerenv``).

The probes are deliberately **generic across distributions and
OpenSSL/CPython builds** — no file paths, no version checks, no
distro-specific commands. Two layers, first conclusive answer wins:

1. **Provider-aware in-process** (``_hashlib``): a non-approved digest
   (MD5) must be *rejected* on the OpenSSL fetch path while an approved
   one (SHA-256) stays available. This is the only reliable in-process
   signal: plain ``hashlib.md5()`` can NEVER be used, because CPython
   silently falls back to its built-in ``_md5`` when OpenSSL rejects the
   digest (PEP 452's ``usedforsecurity`` design) — verified empirically
   in #2570.
2. **``openssl`` CLI**: ``openssl list -digest-algorithms -propquery
   'fips=yes'`` must list a SHA-2 digest and not list MD5.

If neither layer can run (no ``_hashlib`` and no ``openssl`` binary),
the result is ``ok=False`` with a ``no-probe-available`` detail — a
FIPS posture that cannot be verified is treated as absent (fail
closed), and the detail tells the image builder what to provide.

**Dual-maintenance note** (#2626 review): :data:`PROBE_SCRIPT` (the
self-contained snippet run inside containers via ``python3 -c``)
deliberately re-implements layer 1 (``probe_hashlib``) and layer 2
(``parse_openssl_list`` + the CLI argv) because a ``python3 -c``
script cannot import this module from the klangkd host. A fix to the
probe *logic* must be applied in **both** places — and to
``test_fips.py``'s mirror of the script. The single source of truth
for the *policy* (what counts as enforcing, fail-closed on
non-verifiable) is this module's docstring.
"""

import logging
import os
import ssl
import subprocess

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)

# A short sentinel payload for the digest probes.
_PROBE_BYTES = b"klangk-fips-probe"


def parse_openssl_list(stdout: str) -> tuple[bool, str] | None:
    """Parse ``openssl list -digest-algorithms -propquery fips=yes`` output.

    Returns ``(ok, detail)`` when conclusive: ok when a SHA-2 digest is
    in the approved set and MD5 is not; not-ok otherwise. ``None`` when
    the output cannot be interpreted (caller treats as inconclusive).
    """
    low = stdout.lower()
    has_sha2 = "sha256" in low or "sha2-256" in low or "sha512" in low
    has_md5 = "md5" in low
    if not low.strip():
        return None
    if has_sha2 and not has_md5:
        return True, "approved set has SHA-2 and no MD5"
    if has_md5:
        return False, "MD5 appears in the fips=yes approved set"
    if not has_sha2:
        return False, "no SHA-2 digest in the fips=yes approved set"
    return None  # pragma: no cover — unreachable by construction


def run_openssl_list() -> tuple[bool, str]:
    """Layer-2 probe via the ``openssl`` CLI (same process or a container).

    Why the host's openssl matters: it dynamically links the *same*
    libcrypto.so that klangkd's python uses (auth PBKDF2, JWT HMAC,
    outbound TLS), so its provider state answers for the process's
    crypto even on interpreters where layer 1 cannot run. It is the
    fallback for the process probe AND the embedded script's fallback
    inside containers — one parse helper serves both.

    Returns ``(ok, detail)``; ok is False when the CLI is missing,
    fails, or reports a non-FIPS posture.
    """
    try:
        out = subprocess.run(
            [
                "openssl",
                "list",
                "-digest-algorithms",
                "-propquery",
                "fips=yes",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"openssl-cli-unavailable ({type(e).__name__})"
    if out.returncode != 0:
        return (
            False,
            f"openssl-cli-failed rc={out.returncode}: "
            f"{out.stderr.strip()[:120]}",
        )
    parsed = parse_openssl_list(out.stdout)
    if parsed is None:
        return False, "openssl-cli-output-unparseable"
    return parsed


def probe_hashlib() -> tuple[bool, str] | None:
    """Layer-1 probe via CPython's ``_hashlib`` (provider-aware).

    Why the host's hashlib matters at all: klangkd's own crypto runs
    through it — ``hashlib.pbkdf2_hmac("sha512", ...)`` is the password
    KDF on every login (auth.py), and JWT HS256 signing verifies through
    the same OpenSSL. Probing ``openssl_md5`` tests the exact
    provider-gated EVP fetch path those real operations will use;
    ``hashlib.md5()`` cannot (CPython's builtin fallback bypasses the
    provider entirely — see the module docstring).

    Returns ``(ok, detail)`` when conclusive, ``None`` when this layer
    cannot run on the current interpreter (no ``_hashlib``, or the named
    constructors are absent — both possible on exotic builds; the caller
    falls through to the CLI layer).
    """
    # _hashlib is a CPython internal, absent on exotic builds — the
    # deferred import probes THIS interpreter's OpenSSL binding.
    try:
        import _hashlib  # allow-deferred-import
    except ImportError:
        return None
    md5 = getattr(_hashlib, "openssl_md5", None)
    sha256 = getattr(_hashlib, "openssl_sha256", None)
    if md5 is None or sha256 is None:
        return None
    try:
        md5(_PROBE_BYTES)
    except ValueError:
        # Non-approved digest rejected on the OpenSSL fetch path — the
        # FIPS enforcement signal. An approved digest must still work,
        # else the whole fetch path is broken (fail closed).
        try:
            sha256(_PROBE_BYTES)
        except ValueError:
            return False, "md5 rejected but SHA-256 unavailable too"
        return True, "md5 rejected, SHA-256 available (OpenSSL fetch path)"
    except Exception:
        # An unexpected error is not a FIPS signal — inconclusive here,
        # fall through to the CLI layer.
        return None
    return False, "md5 not rejected — FIPS provider not enforcing"


def probe_process() -> tuple[bool, str]:
    """Probe the current process's OpenSSL for active FIPS enforcement.

    "The current process" is klangkd itself, whose OpenSSL serves the
    password KDF (hashlib.pbkdf2_hmac, auth.py) and JWT HMAC-SHA256 —
    see verify_process_fips for why that is audited rather than gated.

    Layer 1 (``_hashlib``) then layer 2 (``openssl`` CLI). Returns
    ``(ok, detail)``; ``detail`` names the layer that decided and why —
    it rides the audit log and any failure error.
    """
    result = probe_hashlib()
    if result is not None:
        return result
    return run_openssl_list()


# The in-container probe: a self-contained python snippet with the same
# two layers, run inside a workspace container via ``podman exec
# python3 -c``. It prints exactly one token line — ``ok:<why>`` /
# ``fail:<why>`` / ``unknown:<why>`` — parsed by probe_container().
# Written for genericity: it requires only *some* probe to exist in the
# image (CPython-with-OpenSSL or the ``openssl`` CLI), never a specific
# distro, OpenSSL version, or module path.
PROBE_SCRIPT = r"""
import subprocess, sys

PAYLOAD = b"klangk-fips-probe"


def layer1():
    try:
        import _hashlib
    except ImportError:
        return None
    md5 = getattr(_hashlib, "openssl_md5", None)
    sha256 = getattr(_hashlib, "openssl_sha256", None)
    if md5 is None or sha256 is None:
        return None
    try:
        md5(PAYLOAD)
    except ValueError:
        try:
            sha256(PAYLOAD)
        except ValueError:
            return "fail:md5 rejected but SHA-256 unavailable too"
        return "ok:md5 rejected, SHA-256 available (OpenSSL fetch path)"
    return "fail:md5 not rejected — FIPS provider not enforcing"


def layer2():
    try:
        out = subprocess.run(
            ["openssl", "list", "-digest-algorithms", "-propquery",
             "fips=yes"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return "unknown:openssl-cli-unavailable (%s)" % type(e).__name__
    if out.returncode != 0:
        return "fail:openssl-cli-failed rc=%d" % out.returncode
    low = out.stdout.lower()
    if not low.strip():
        return "unknown:openssl-cli-output-empty"
    if "md5" in low:
        return "fail:MD5 appears in the fips=yes approved set"
    if not ("sha256" in low or "sha2-256" in low or "sha512" in low):
        return "fail:no SHA-2 digest in the fips=yes approved set"
    return "ok:approved set has SHA-2 and no MD5"


r = layer1()
if r is None:
    r = layer2()
print(r)
sys.exit(0)
"""


def _cli_probe_cmd() -> list[str]:
    """The layer-2 command run inside the container when python3 is absent."""
    return [
        "openssl",
        "list",
        "-digest-algorithms",
        "-propquery",
        "fips=yes",
    ]


def _missing_binary(stderr: str) -> bool:
    """Best-effort "the exec failed because the binary is absent" test.

    Matches the C-locale / English shells' "not found" family. A
    non-English container (LANG=de_DE → "nicht gefunden") misses, so
    the python3→CLI fallback doesn't trigger — but the miss is safe:
    the caller then fails closed with ``probe-exec-failed`` (a refusal
    with a slightly less precise detail, never an acceptance) (#2626
    review).
    """
    low = stderr.lower()
    return (
        "not found" in low or "no such file" in low or "not a directory" in low
    )


async def probe_container(podman, container_id: str) -> tuple[bool, str]:
    """Probe a workspace container for active FIPS enforcement.

    Runs the python probe script inside the container; when the image
    has no python3, falls back to the ``openssl`` CLI directly. Never
    raises — a probe that cannot run is an ``ok=False`` result (fail
    closed) with a detail explaining what the image lacks.
    """
    try:
        rc, out, err = await podman.exec_container(
            container_id, ["python3", "-c", PROBE_SCRIPT], timeout=30
        )
    except Exception as e:  # podman-level failure — treat as no-python
        rc, out, err = -1, "", f"{type(e).__name__}: {e}"
    if rc == 0:
        return _parse_probe_output(out)
    # No usable python3 in the image — try the openssl CLI directly.
    if not _missing_binary(err):
        return False, f"probe-exec-failed rc={rc}: {err.strip()[:120]}"
    return await _probe_via_openssl_cli(podman, container_id)


def _parse_probe_output(out: str) -> tuple[bool, str]:
    """The python probe script's last-line verdict (ok:/fail:/unknown:)."""
    line = out.strip().splitlines()[-1] if out.strip() else ""
    if line.startswith("ok:"):
        return True, line[3:]
    if line.startswith("fail:"):
        return False, line[5:]
    if line.startswith("unknown:"):
        return False, line[8:]
    return False, f"probe-output-unparseable: {line[:120]!r}"


async def _probe_via_openssl_cli(
    podman, container_id: str
) -> tuple[bool, str]:
    """The openssl-CLI fallback probe for images without python3."""
    try:
        rc, out, err = await podman.exec_container(
            container_id, _cli_probe_cmd(), timeout=30
        )
    except Exception as e:
        # podman already worked once to reach this point, so any failure
        # here is a probe-infrastructure hiccup, not a FIPS answer.
        return False, f"openssl-cli-exec-failed: {type(e).__name__}: {e}"
    if rc != 0:
        if _missing_binary(err):
            return (
                False,
                "no-probe-available: image has neither python3-with-"
                "OpenSSL nor the openssl CLI",
            )
        return False, f"openssl-cli-failed rc={rc}: {err.strip()[:120]}"
    parsed = parse_openssl_list(out)
    if parsed is None:
        return False, "openssl-cli-output-unparseable"
    return parsed


def running_in_container() -> bool:
    """True when klangkd itself runs inside a container (#2628).

    The standard OCI marker files — ``/.dockerenv`` (docker, podman,
    most runtimes) and ``/run/.containerenv`` (podman's systemd-era
    addition). On a control host neither exists; inside the docker
    host-container image one always does.

    Decides the ``KLANGKD_FIPS_MODE`` process-probe posture: a failed
    probe warns on a control host (the operator's OpenSSL) but aborts
    the boot in a containerized deployment (an image we ship — a
    non-FIPS klangkd container in FIPS mode is a misconfiguration, not
    an environment difference).
    """
    return os.path.exists("/.dockerenv") or os.path.exists(
        "/run/.containerenv"
    )


def verify_process_fips(settings) -> None:
    """Startup check for ``KLANGKD_FIPS_MODE`` (#2570 Part 2, #2628).

    Why probe the host process at all (vs only the workspace
    containers): klangkd itself performs crypto on this machine —
    PBKDF2-HMAC-SHA512 on every login (``hashlib.pbkdf2_hmac``),
    HMAC-SHA256 on every JWT sign/verify, and outbound TLS via the
    ``ssl`` module — all routed through the host process's OpenSSL,
    outside any workspace container.

    Posture (#2628):

    - **Containerized backend** (``running_in_container()``): the
      process OpenSSL is the crypto boundary of an image *we* ship — a
      failed probe raises :class:`ConfigurationError` and the boot
      aborts.
    - **Control host**: a failed probe logs a prominent warning and
      the boot continues — klangkd may legitimately run on a host
      whose OpenSSL is not the FIPS variant while every workspace it
      launches is (workspaces stay fail-closed at their start gate).

    Either way a *passing* probe is logged at info with the OpenSSL
    version — the audit line an assessor looks for.
    """
    if not getattr(settings, "fips_mode", False):
        return
    version = ssl.OPENSSL_VERSION
    ok, detail = probe_process()
    if ok:
        logger.info(
            "FIPS mode enabled: OpenSSL %s, provider enforcement verified "
            "(%s)",
            version,
            detail,
        )
        return
    if running_in_container():
        logger.error(
            "KLANGKD_FIPS_MODE is enabled but the klangkd process's "
            "OpenSSL (%s) is NOT FIPS-enforcing (%s). klangkd is running "
            "inside a container, where its own OpenSSL is the crypto "
            "boundary (password hashing, JWT signing, outbound TLS) — "
            "refusing to start. Use the FIPS host image "
            "(klangk:build-fips-host-image / Dockerfile.fips) or turn "
            "off KLANGKD_FIPS_MODE.",
            version,
            detail,
        )
        raise ConfigurationError(
            "KLANGKD_FIPS_MODE: containerized backend's OpenSSL is not "
            f"FIPS-enforcing ({detail})"
        )
    logger.warning(
        "KLANGKD_FIPS_MODE is enabled but the klangkd process's OpenSSL "
        "(%s) is NOT FIPS-enforcing (%s). Workspace containers will still "
        "be probed and failed closed; run klangkd under an OpenSSL with "
        "the FIPS provider active (see docs/deployment/fips.md) for a "
        "fully validated posture.",
        version,
        detail,
    )
