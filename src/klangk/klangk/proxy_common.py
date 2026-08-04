"""Pure helpers shared by the reverse-proxy engines (#1642).

These historically lived in :mod:`klangk.proxy` (the nginx engine); they are
extracted here so the nginx engine can be removed in 2.X without taking the
Caddy engine's dependencies with it. Both engines use them unchanged:

- host-IPv4 auto-detection (:func:`detect_host_ipv4s`) and the loopback probe
  (:func:`_is_loopback`), plus the fallback ACL/deny subnets used when
  auto-detection yields nothing;
- :func:`_proxy_preexec` — the fork-time setup (new session for ``killpg`` +
  ``PR_SET_PDEATHSIG``) shared by every engine's child-process watchdog.

Nothing here reads settings or renders config — pure helpers only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import ipaddress
import os
import signal
import subprocess
import sys


# Loopback ranges excluded from the catch-all deny (CONTAINER_DENY) — local
# browsers connect via loopback and must reach the full UI/API. Matches the
# ``_is_loopback`` helper in the old nginx.sh (127.0.0.0/8 + ::1).
_LOOPBACK_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _is_loopback(addr: str) -> bool:
    """True for any address in 127.0.0.0/8 or ::1."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _LOOPBACK_NETS)


def detect_host_ipv4s() -> list[str]:
    """Auto-detect this host's IPv4 addresses (the pasta-NAT container source set).

    Podman rootless default (pasta) shares the host network via userspace NAT,
    so container traffic to ``host.containers.internal`` arrives from the
    host's own IPv4. ``ip -4 addr show`` lists them (including 127.0.0.1 from
    ``lo`` — wanted for CONTAINER_ACL, filtered out of CONTAINER_DENY below).
    Returns ``[]`` on failure (caller falls back to RFC1918 ranges).
    """
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return []
    addrs: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            # "inet 192.168.1.5/24 ..."
            token = line.split()[1]
            ip = token.split("/")[0]
            addrs.append(ip)
    return addrs


# Fallback subnets when auto-detection yields nothing (mirrors nginx.sh):
# 172.16/12 + 10/8 (common container ranges), explicitly NOT 192.168/16
# (most common LAN range — allowing it would expose the LLM proxy to peers).
_FALLBACK_ACL_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8", "127.0.0.1"]
_FALLBACK_DENY_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8"]


# ---------------------------------------------------------------------------
# Fork-time preexec (shared by every engine's child-process watchdog)
# ---------------------------------------------------------------------------

_PR_SET_PDEATHSIG = 1
_HAS_PDEATHSIG = sys.platform == "linux"
if _HAS_PDEATHSIG:  # pragma: no cover  – linux-only
    _libc = ctypes.CDLL(
        ctypes.util.find_library("c") or "libc.so.6", use_errno=True
    )


def _proxy_preexec() -> None:  # pragma: no cover  – runs in forked child
    """New session (for killpg) + auto-SIGTERM when parent dies (#1533).

    ``os.setsid()`` puts the proxy child in its own process group so ``stop()``
    can ``os.killpg`` the entire tree on clean shutdown.

    On Linux, ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` asks the kernel to send
    SIGTERM to the proxy child if klangkd dies without calling ``stop()``
    (e.g. SIGKILL).  The child forwards SIGTERM to its workers, so the whole
    tree exits.  macOS has no equivalent; on unclean shutdown, orphaned proxy
    processes must be cleaned up externally.
    """
    os.setsid()
    if _HAS_PDEATHSIG:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
