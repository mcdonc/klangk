"""FIPS mode enforcement: generic activation probes (#2570, #2591).

``KLANGKD_FIPS_MODE`` makes klangkd verify — and refuse to run without —
an actively-enforcing OpenSSL FIPS provider:

- at every workspace-container start (the single
  :meth:`~klangk.container.registry.ContainerRegistry._create_and_start`
  choke point), probing inside the container and failing closed — the
  container is removed and the start raises;
- at backend startup, probing klangkd's own process (its password
  hashing and JWT signing go through this OpenSSL) and logging the
  result for audit — a warning, not a boot abort, since klangkd may
  legitimately run on a control host whose OpenSSL is not the FIPS
  variant while every workspace it launches is.

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
"""

import logging
import ssl
import subprocess

logger = logging.getLogger(__name__)

# A short sentinel payload for the digest probes.
_PROBE_BYTES = b"klangk-fips-probe"


def _parse_openssl_list(stdout: str) -> tuple[bool, str] | None:
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


def _run_openssl_list() -> tuple[bool, str]:
    """Layer-2 probe via the ``openssl`` CLI (same process or a container).

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
    parsed = _parse_openssl_list(out.stdout)
    if parsed is None:
        return False, "openssl-cli-output-unparseable"
    return parsed


def _probe_hashlib() -> tuple[bool, str] | None:
    """Layer-1 probe via CPython's ``_hashlib`` (provider-aware).

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

    Layer 1 (``_hashlib``) then layer 2 (``openssl`` CLI). Returns
    ``(ok, detail)``; ``detail`` names the layer that decided and why —
    it rides the audit log and any failure error.
    """
    result = _probe_hashlib()
    if result is not None:
        return result
    return _run_openssl_list()


# The in-container probe: a self-contained python snippet with the same
# two layers, run inside a workspace container via ``podman exec
# python3 -c``. It prints exactly one token line — ``ok:<why>`` /
# ``fail:<why>`` / ``unknown:<why>`` — parsed by probe_container().
# Written for genericity: it requires only *some* probe to exist in the
# image (CPython-with-OpenSSL or the ``openssl`` CLI), never a specific
# distro, OpenSSL version, or module path.
_PROBE_SCRIPT = r"""
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
    """True when an exec failed because the binary is not in the image."""
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
            container_id, ["python3", "-c", _PROBE_SCRIPT], timeout=30
        )
    except Exception as e:  # podman-level failure — treat as no-python
        rc, out, err = -1, "", f"{type(e).__name__}: {e}"
    if rc == 0:
        line = out.strip().splitlines()[-1] if out.strip() else ""
        if line.startswith("ok:"):
            return True, line[3:]
        if line.startswith("fail:"):
            return False, line[5:]
        if line.startswith("unknown:"):
            return False, line[8:]
        return False, f"probe-output-unparseable: {line[:120]!r}"
    # No usable python3 in the image — try the openssl CLI directly.
    if not _missing_binary(err):
        return False, f"probe-exec-failed rc={rc}: {err.strip()[:120]}"
    try:
        rc, out, err = await podman.exec_container(
            container_id, _cli_probe_cmd(), timeout=30
        )
    except Exception as e:  # pragma: no cover — podman already worked once
        return False, f"openssl-cli-exec-failed: {type(e).__name__}: {e}"
    if rc != 0:
        if _missing_binary(err):
            return (
                False,
                "no-probe-available: image has neither python3-with-"
                "OpenSSL nor the openssl CLI",
            )
        return False, f"openssl-cli-failed rc={rc}: {err.strip()[:120]}"
    parsed = _parse_openssl_list(out)
    if parsed is None:
        return False, "openssl-cli-output-unparseable"
    return parsed


def verify_process_fips(settings) -> None:
    """Startup audit check for ``KLANGKD_FIPS_MODE`` (#2570 Part 2).

    The *enforcement* point is the workspace containers (probed at every
    start in the registry, fail closed). This process-level check is the
    audit half: klangkd's own OpenSSL (password hashing, JWT signing)
    is probed and the result logged with the OpenSSL version —
    verified-and-active, or a prominent warning when not, since klangkd
    may legitimately run on a control host whose OpenSSL is not the
    FIPS variant while every workspace it launches is.
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
    logger.warning(
        "KLANGKD_FIPS_MODE is enabled but the klangkd process's OpenSSL "
        "(%s) is NOT FIPS-enforcing (%s). Workspace containers will still "
        "be probed and failed closed; run klangkd under an OpenSSL with "
        "the FIPS provider active (see docs/deployment/fips.md) for a "
        "fully validated posture.",
        version,
        detail,
    )
