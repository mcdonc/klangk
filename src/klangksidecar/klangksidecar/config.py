"""Env-derived configuration for the network sidecar's DNS proxy (#2450).

Constants are read once at import from the KLANGKNETWORK_EGRESS_* /
KLANGKNETWORK_* env vars; duration_ttl maps a consent duration token
(#2328) to seconds.
"""

from __future__ import annotations

import logging
import os

# The `websockets` client logs the full HTTP request line (incl. any ?token=
# query param) at DEBUG; cap it at WARNING so a workspace JWT can't leak to
# sidecar stdout/logs even if debug logging is enabled elsewhere (#2309).
logging.getLogger("websockets").setLevel(logging.WARNING)


UPSTREAM = (os.environ.get("KLANGKNETWORK_EGRESS_UPSTREAM", "8.8.8.8"), 53)


LISTEN_PORT = int(os.environ.get("KLANGKNETWORK_EGRESS_LISTEN_PORT", "15353"))


IPT = os.environ.get("KLANGKNETWORK_IPTABLES", "iptables")


DEBUG = bool(os.environ.get("KLANGKNETWORK_EGRESS_DEBUG"))


# fwmark the proxy stamps on its upstream socket so the sidecar's nat/filter
# rules (a) exempt the proxy's forwards from the :53 REDIRECT (loop-avoidance)
# and (b) allow only marked packets to reach the upstream. The workspace lacks
# CAP_NET_RAW/NET_ADMIN so it cannot mark — its :53 traffic is redirected here
# and allow-listed, closing the direct-to-upstream exfil bypass (#2264). Must
# match entrypoint.sh's KLANGKNETWORK_EGRESS_MARK.
MARK = int(os.environ.get("KLANGKNETWORK_EGRESS_MARK", "75"))


# Learned-IP housekeeping (#2256).
SWEEP_INTERVAL = float(os.environ.get("KLANGKNETWORK_EGRESS_SWEEP_INTERVAL", "5"))


MIN_TTL = float(os.environ.get("KLANGKNETWORK_EGRESS_MIN_TTL", "30"))


# --- interactive consent hold (#2311 half B): when a consent endpoint is set,
# the proxy holds denied egress pending a verdict over the egress-sidecar WS.
QUEUE_NUM = int(os.environ.get("KLANGKNETWORK_EGRESS_NFQUEUE_NUM", "5139"))


CONSENT_URL = os.environ.get("KLANGKNETWORK_EGRESS_CONSENT_URL", "")


# How long to await a verdict before fail-closing to deny. The consent gate is
# the connection SYN (NFQUEUE), so this can match the kernel's connect timeout
# (tcp_syn_retries ~= 127s) -- far longer than a DNS resolver's <=30s getaddrinfo
# cap (#2324). Should be >= klangkd's consent hold timeout so the sidecar is
# still waiting when the coordinator expires the hold (and returns deny/expired).
HOLD_TIMEOUT = float(os.environ.get("KLANGKNETWORK_EGRESS_HOLD_TIMEOUT", "120"))


# How long to reuse a SYN verdict for a (ip, port) flow. The kernel retransmits
# a held SYN (tcp_syn_retries); without reuse each retransmit would re-prompt.
# After an allow the IP is also learned (ACCEPT), so new SYNs stop hitting
# NFQUEUE -- this only covers retransmits that queued during the hold.
VERDICT_CACHE_TTL = float(
    os.environ.get("KLANGKNETWORK_EGRESS_VERDICT_CACHE_TTL", "120")
)


# Min seconds between idle-activity reports to klangkd (#2479): the sidecar
# bumps the workspace's idle timer on egress/network activity (a DNS query or a
# queued connection SYN) so an egress-only workload -- whose traffic bypasses
# klangkd entirely -- is not reaped by the idle timeout. Flood-gated: at most
# one frame per workspace per interval (jittered to 0.5x-1.0x of the base so
# workspaces don't herd onto a shared send cadence), so a connect-heavy workload
# (a build, a crawler) does not spam the daemon. The first event after a quiet
# period (>= the jittered interval since the last send) forwards at once, so a
# single connect after a long idle stretch resets the timer promptly.
ACTIVITY_GATE_S = float(os.environ.get("KLANGKNETWORK_EGRESS_ACTIVITY_GATE", "60"))


# How long a deny keeps its REJECT (tcp-reset) rule so the denied connection
# fails fast (ECONNREFUSED) instead of waiting for tcp_syn_retries (~127s).
# Only needs to catch the SYN retransmit (~1 RTO); the verdict cache separately
# keeps the deny from re-prompting for VERDICT_CACHE_TTL.
CONSENT_REJECT_TTL = float(os.environ.get("KLANGKNETWORK_EGRESS_REJECT_TTL", "10"))


# Opt-in per-RST debug logging (#2464): the forged eager-deny RST is the
# primary fast-refuse, so when a denied connection instead *times out* this
# logs each forged RST (socket open? sendto ok? the 4-tuple) to the sidecar's
# stdout. Off by default -- a denied connection's SYN retransmits each hit the
# cached deny and re-forge an RST, which would spam a production sidecar's log.
# The egress smoketest enables it (KLANGKNETWORK_EGRESS_DEBUG_RST=1, forwarded
# by ContainerManager.start_network_sidecar) and captures the sidecar's podman
# log so a fast-refuse miss is diagnosable after the run.
RST_DEBUG = os.environ.get("KLANGKNETWORK_EGRESS_DEBUG_RST", "") == "1"


# Duration token -> seconds the sidecar honors a verdict (#2328): an allow
# learns the IP for T; a deny REJECTs for T. `once` = this connection only (no
# learn; a short deny). `restart` = the container's lifetime (the sidecar's
# in-memory rules); `forever` = the workspace's lifetime -- at the sidecar level
# both map to a long in-memory TTL, but `forever`'s real distinction is that
# klangkd persists it across sidecar restarts: an allow is appended to the
# workspace's `allowed_domains`, which this sidecar re-reads on start
# (#2368), so the allow survives a container restart. (That cross-restart
# persistence is #2368's `forever`-allow sub-piece; the deny counterpart is
# #2369.)
_DURATION_SECONDS = {
    "5s": 5,  # test-only (#2363); TEMPORARILY UI-offered for manual testing (#2465)
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
    "1w": 604800,
}


DURATION_FOREVER = 365 * 86400  # ~a year; practically until restart


def duration_ttl(duration: str) -> float | None:
    """Seconds for a timed/tilrestart/forever duration, or None for ``once``."""
    if duration in _DURATION_SECONDS:
        return _DURATION_SECONDS[duration]
    if duration in ("tilrestart", "forever"):
        return DURATION_FOREVER
    return None  # "once" or unknown -> caller handles (no learn / short reject)


# The workspace JWT (rotated) is bind-mounted here read-only; read fresh on each
# (re)connect so rotation is picked up (#2242, #2311). Not baked in env because
# the workspace token expires and rotates.
WORKSPACE_TOKEN_PATH = "/run/klangk/workspace-token"
