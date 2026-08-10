#!/usr/bin/env python3
"""FQDN egress DNS proxy — runs in the network sidecar (#2250, #2253).

The sidecar shares the workspace's network namespace (the workspace runs
``--network container:<this sidecar>``). The sidecar's ``entrypoint.sh`` installs
a nat REDIRECT of the workspace's configured DNS resolvers (:53) to this proxy's
listen port; this proxy applies an FQDN allow-list, forwards allowed queries to a
*different* upstream (so the REDIRECT does not loop), learns the A-record IPs from
the responses, and inserts ``iptables -I OUTPUT 1 -d <ip> [-p tcp --dport <p>] -j
ACCEPT`` for each so the workspace can reach exactly the IPs it resolved — solving
DNS round-robin. Denied names get NXDOMAIN.

DNS wire parsing is delegated to **dnspython** (rather than hand-rolled byte
slicing) so EDNS, CNAME chains, TCP-sized responses, and malformed packets are
handled correctly — a parser bug in a security component is dangerous, and a
maintained library removes that risk.

Allow-list semantics (#2256):

- **Per-domain port scoping**: ``github.com:443`` allows only ``:443`` to
  github's learned IPs; ``github.com`` (no port) allows all ports. The port
  is taken from the spec that matched the queried name.
- **Wildcards**: ``*.pypi.org`` matches subdomains of ``pypi.org`` only (NOT
  the apex ``pypi.org`` itself); a bare ``pypi.org`` matches the apex + all
  subdomains. A single learned IP inherits the union of ports from every
  matching spec, and any port-less matching spec means all-ports.
- **Learned-IP TTL/cleanup**: each learned IP is allowed only for the TTL of
  the DNS response that resolved it; a background sweeper removes the ACCEPT
  rule once the TTL elapses so stale IPs do not linger.

Interactive consent hold (#2311 half B): when a consent endpoint is configured,
the proxy is an **asyncio** program that keeps one persistent WebSocket to
klangkd's ``/ws/egress-sidecar`` and, for each *denied* destination, sends an
``{type:egress, id, dst, dport}`` frame and *suspends* the query pending the
matching ``{type:verdict, id, decision}`` verdict instead of NXDOMAIN-ing at
once. The coordinator (klangkd) gate-checks: a registered decider -> the request
is held; otherwise it returns ``deny`` fast (the proxy NXDOMAIN/DROPs as before
-- no behavior change for static workspaces). An ``allow`` verdict lets the held
query proceed (DNS: resolve upstream + learn the IPs; NFQUEUE: ``accept`` the
packet, and conntrack's ``ESTABLISHED,RELATED`` rule passes the rest of the
connection). **Fail-close**: a down WebSocket or a timed-out verdict resolves to
``deny`` immediately, so the workspace stays locked down when klangkd or the
decider is unreachable (today's static behavior) — no new latency, no hang.

Configuration (env):
  KLANGKNETWORK_EGRESS_ALLOW       comma-separated allow-list: ``host[:port]``,
                            ``*.domain[:port]``, or CIDR specs. CIDR specs are
                            applied statically by the entrypoint; this proxy
                            matches only the host/wildcard specs.
  KLANGKNETWORK_EGRESS_UPSTREAM    the real upstream resolver the proxy forwards to
                            (default ``8.8.8.8``). MUST differ from the
                            workspace's configured (redirected) resolvers or the
                            proxy's forwards loop back into itself.
  KLANGKNETWORK_EGRESS_LISTEN_PORT UDP port to listen on (default ``15353``).
  KLANGKNETWORK_IPTABLES    iptables binary (default ``iptables``).
  KLANGKNETWORK_EGRESS_DEBUG       if set, log each allow/deny decision.
  KLANGKNETWORK_EGRESS_MARK  fwmark for the proxy's upstream socket (default 75;
                            must match entrypoint.sh).
  KLANGKNETWORK_EGRESS_SWEEP_INTERVAL  seconds between TTL-expiry sweeps (default 5).
  KLANGKNETWORK_EGRESS_MIN_TTL  floor for a learned IP's lifetime so a 0-TTL
                            response does not immediately yank the rule the
                            workspace needs to reach the IP it just resolved
                            (default 30).
  KLANGKNETWORK_EGRESS_CONSENT_URL  klangkd consent endpoint (HTTP). When set,
                            the proxy opens the egress-sidecar WS + holds denied
                            egress pending a verdict. Empty -> static
                            NXDOMAIN/DROP (today's behavior; consent disabled).
  KLANGKNETWORK_EGRESS_HOLD_TIMEOUT  seconds to await a verdict before
                            fail-closing to deny (default 30). Should be >=
                            klangkd's consent hold timeout; a DNS resolver may
                            give up first (~10s), in which case a slower verdict
                            effectively denies the query.
  KLANGKNETWORK_EGRESS_HOLD_LIMIT    max concurrent DNS-path holds in flight
                            (default 32); a flood past this fail-closes to
                            NXDOMAIN.

Limitations: transport is UDP only (TCP fallback is a future addition).
"""

import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time
import uuid

import dns.message
import dns.rcode
import dns.rdatatype

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
# How long to await a verdict before fail-closing to deny. Should be >= klangkd's
# consent hold timeout so the sidecar is still waiting when the coordinator
# expires the hold (and returns deny/expired).
HOLD_TIMEOUT = float(os.environ.get("KLANGKNETWORK_EGRESS_HOLD_TIMEOUT", "30"))
# Bounds concurrent DNS-path holds so a flooding workspace can't exhaust the
# proxy (each hold occupies a task + a slot for up to HOLD_TIMEOUT). The NFQUEUE
# path is bounded separately by the kernel queue length + the iptables
# rate-limit in entrypoint.sh; overflows DROP (fail-close).
HOLD_LIMIT = int(os.environ.get("KLANGKNETWORK_EGRESS_HOLD_LIMIT", "32"))
# The workspace JWT (rotated) is bind-mounted here read-only; read fresh on each
# (re)connect so rotation is picked up (#2242, #2311). Not baked in env because
# the workspace token expires and rotates.
WORKSPACE_TOKEN_PATH = "/run/klangk/workspace-token"


def parse_specs() -> list[tuple[str, int | None, bool]]:
    """Structured allow-list specs from ``KLANGKNETWORK_EGRESS_ALLOW``.

    Each entry is ``(host, port, is_wildcard)`` where ``port`` is ``None``
    (all ports) and ``is_wildcard`` means ``*.host`` (subdomains only). CIDR
    specs (``10.0.0.0/8``) are excluded — the entrypoint applies those
    statically. The grammar mirrors ``klangk.netfilter.parse_allowed_domains``
    so the API and the sidecar agree on what a spec means (#2256).
    """
    out: list[tuple[str, int | None, bool]] = []
    for spec in os.environ.get("KLANGKNETWORK_EGRESS_ALLOW", "").split(","):
        spec = spec.strip()
        if not spec or "/" in spec:
            continue
        port: int | None = None
        if ":" in spec:
            host_part, port_part = spec.rsplit(":", 1)
            if port_part.isdigit():
                port = int(port_part)
                spec = host_part
        host = spec.lower()
        is_wildcard = host.startswith("*.")
        if is_wildcard:
            host = host[2:]
        if host:
            out.append((host, port, is_wildcard))
    return out


SPECS = parse_specs()

# Learned IPs: {ip: {"expire": epoch, "ports": set[int | None]}}. A ``None``
# in ``ports`` is the all-ports ACCEPT rule. Guarded by _LOCK because the
# asyncio loop runs the iptables installs + sweeps in the default thread-pool
# executor (see _learn_all / _async_sweeper), so two worker threads can touch
# _LEARNED at once; the lock serializes the rule+record mutations.
_LEARNED: dict[str, dict] = {}
_LOCK = threading.Lock()
# Strong refs to background asyncio tasks (DNS holds + the TTL sweeper) so
# CPython doesn't GC a task that's awaiting a verdict / sleeping. A done-callback
# discards each entry when its task completes.
_BG_TASKS: set[asyncio.Task] = set()


def query_name(wire: bytes) -> str:
    """The queried domain (lowercased, no trailing dot) from a DNS wire message."""
    msg = dns.message.from_wire(wire)
    if not msg.question:
        return ""
    return msg.question[0].name.to_text().rstrip(".").lower()


def a_records_with_ttl(wire: bytes) -> list[tuple[str, int]]:
    """``[(ip, ttl_seconds), ...]`` from a DNS response wire.

    Walks the answer section (CNAME chains are transparent — the A records
    for the canonical name are in the answer too) and returns each A-record
    address with its rrset TTL. The TTL drives learned-IP expiry (#2256).
    """
    msg = dns.message.from_wire(wire)
    out: list[tuple[str, int]] = []
    for rrset in msg.answer:
        if rrset.rdtype == dns.rdatatype.A:
            ttl = int(rrset.ttl)
            for rdata in rrset:
                out.append((rdata.address, ttl))
    return out


def nxdomain_for(wire: bytes) -> bytes:
    """An NXDOMAIN response wire for the given query wire."""
    query = dns.message.from_wire(wire)
    resp = dns.message.make_response(query)
    resp.set_rcode(dns.rcode.NXDOMAIN)
    return resp.to_wire()


def ports_for(qname: str) -> set[int] | None:
    """The ports a queried name is allowed on under :data:`SPECS`.

    ``None``  — a port-less spec matched (allow all ports).
    ``set()`` — nothing matched (deny).
    ``{443, ...}`` — allow exactly these TCP ports.

    A bare host matches the apex + subdomains; a ``*.host`` wildcard matches
    subdomains only (the apex is deliberately excluded so ``*.pypi.org`` and
    ``pypi.org`` are distinct, non-redundant scopes) (#2256).
    """
    ports: set[int] = set()
    for host, port, is_wildcard in SPECS:
        if is_wildcard:
            matched = qname.endswith("." + host)
        else:
            matched = qname == host or qname.endswith("." + host)
        if not matched:
            continue
        if port is None:
            return None  # an all-ports spec dominates
        ports.add(port)
    return ports


def _rule_args(ip: str, port: int | None) -> list[str]:
    """iptables OUTPUT rule args for ``ACCEPT`` to ``ip`` (optionally scoped)."""
    args = ["-d", ip]
    if port is not None:
        args += ["-p", "tcp", "--dport", str(port)]
    args += ["-j", "ACCEPT"]
    return args


def _rule_exists(ip: str, port: int | None) -> bool:
    return (
        subprocess.run(
            [IPT, "-C", "OUTPUT", *_rule_args(ip, port)],
            capture_output=True,
        ).returncode
        == 0
    )


def _install(ip: str, port: int | None) -> None:
    """Insert the ACCEPT rule at the top of OUTPUT if not already present."""
    if _rule_exists(ip, port):
        return
    subprocess.run(
        [IPT, "-I", "OUTPUT", "1", *_rule_args(ip, port)],
        capture_output=True,
    )


def _remove(ip: str, port: int | None) -> None:
    """Delete one matching ACCEPT rule; swallow failure if it's already gone."""
    subprocess.run(
        [IPT, "-D", "OUTPUT", *_rule_args(ip, port)],
        capture_output=True,
    )


def allow(ip: str, port: int | None, ttl: int | float) -> None:
    """Install (if new) the ACCEPT for ``ip[:port]`` and refresh its TTL.

    ``port`` is ``None`` for an all-ports rule. The learned IP's expiry is
    set to ``now + max(ttl, MIN_TTL)`` (a 0-TTL response must not yank the
    rule the workspace needs to reach the IP it just resolved) and only ever
    moves forward, so a shorter-TTL re-resolution can't prematurely expire a
    longer-lived prior rule (#2256).

    The install happens **under** :data:`_LOCK` so the kernel rule and its
    ``_LEARNED`` record are atomic w.r.t. :func:`sweep_once`'s remove+delete
    (also under the lock). Without that, a concurrent sweep running in another
    executor worker could delete a rule :func:`allow` just installed while
    ``_LEARNED`` still records it as present -- a fail-closed availability gap
    that only self-heals on the next re-resolution (#2256 review). ``allow`` and
    :func:`sweep_once` both run off the event loop in the default thread-pool
    executor (see :func:`_learn_all` / :func:`_async_sweeper`), so the lock
    genuinely serializes them; contention is negligible.
    """
    expire = time.time() + max(ttl, MIN_TTL)
    with _LOCK:
        _install(ip, port)
        rec = _LEARNED.get(ip)
        if rec is None:
            _LEARNED[ip] = {"expire": expire, "ports": {port}}
        else:
            rec["expire"] = max(rec["expire"], expire)
            rec["ports"].add(port)


def sweep_once(now: float | None = None) -> list[tuple[str, set]]:
    """Remove learned IPs whose TTL has elapsed; return ``(ip, ports)`` removed.

    Removal runs **under** :data:`_LOCK` (see :func:`allow`): the rule delete
    and the ``_LEARNED`` delete are atomic, so a concurrent :func:`allow`
    can't re-record an IP whose kernel rule was just swept. Factored out of
    :func:`_async_sweeper` so it is unit-testable with a mocked clock and iptables
    (#2256).
    """
    if now is None:
        now = time.time()
    expired: list[tuple[str, set]] = []
    with _LOCK:
        for ip, rec in list(_LEARNED.items()):
            if rec["expire"] > now:
                continue
            ports = set(rec["ports"])
            for port in ports:
                try:
                    _remove(ip, port)
                except Exception:
                    pass  # a transient failure drops one rule, not the sweep
            del _LEARNED[ip]
            expired.append((ip, ports))
    return expired


async def _async_sweeper() -> None:
    """Background task: periodically drop learned IPs past their TTL (#2256).

    :func:`sweep_once` runs in the executor so its iptables ``-D`` forks don't
    block the loop (and so it can run concurrently with :func:`_learn_all`,
    serialized by :data:`_LOCK`).
    """
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            await asyncio.get_running_loop().run_in_executor(None, sweep_once)
        except Exception:
            pass  # a transient sweep failure defers cleanup to the next tick


def _fmt_ports(ports: set[int | None]) -> str:
    return "all" if None in ports else ",".join(sorted(str(p) for p in ports))


def check_mark() -> None:
    """Verify the proxy can set SO_MARK (needs CAP_NET_ADMIN/NET_RAW).

    Without it the proxy's upstream forwards are not exempted from the nat
    REDIRECT and loop back into itself — DNS is broken. Fail loud at startup.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    except OSError as exc:
        raise SystemExit(
            f"dns-proxy: cannot set SO_MARK={MARK} ({exc}); the sidecar needs "
            "CAP_NET_ADMIN for mark-based loop-avoidance (#2264)"
        )
    finally:
        probe.close()


def _learn_all(recs: list[tuple[str, int]], ports: set[int | None]) -> None:
    """Install the ACCEPT rule for each learned IP/port (sync; runs in the
    executor so the iptables forks don't block the event loop)."""
    for ip, ttl in recs:
        for port in ports:
            allow(ip, port, ttl)


async def _respond_allowed(
    s: socket.socket,
    resp: bytes,
    addr: tuple[str, int],
    qname: str,
    ports: set[int | None],
) -> None:
    """Learn the response's IPs (port-scoped, TTL-tracked) + send it, swallowing
    transient errors.

    The iptables installs (:func:`_learn_all`) run off the loop in the default
    thread-pool executor so a burst of learned IPs can't stall the DNS receive
    loop or verdict dispatch. A failure here (a transient ``iptables`` error, or
    a ``sendto`` to a vanished client) must drop only this one response -- not
    kill the proxy (#2278): if it escaped, the sidecar's PID 1 would exit, DNS
    would be dead for the workspace, and the learned ``ACCEPT`` rules would
    persist (a partial fail-open).
    """
    loop = asyncio.get_running_loop()
    try:
        recs = a_records_with_ttl(resp)
    except Exception:
        recs = []
    try:
        if recs:
            await loop.run_in_executor(None, _learn_all, recs, ports)
        if DEBUG:
            print(
                f"allow {qname} -> {[ip for ip, _ in recs]} ports={_fmt_ports(ports)}",
                flush=True,
            )
        s.sendto(resp, addr)
    except Exception:
        pass


def _decision(qname: str, ports: set[int] | None) -> tuple[bool, set[int | None]]:
    """Classify a query: ``(deny, port_set)``.

    ``deny=True`` -> send NXDOMAIN. ``port_set`` is the set of ports to allow
    for each learned IP (a ``None`` entry is the all-ports rule). Factored out
    of :func:`main` so the None-vs-empty gate is unit-tested directly —
    inverting it (treating ``None`` as deny, or an empty set as allow) is a
    fail-open/fail-closed bug in the security gate (#2256).
    """
    if not qname or (ports is not None and not ports):
        return True, set()
    return False, ports if ports is not None else {None}


def parse_dest(payload: bytes) -> tuple[str, int]:
    """``(dst_ip, dst_port)`` from an IPv4 packet payload, or ``("", 0)``.

    The NFQUEUE payload may start at L3 (IP) or include a 14-byte Ethernet
    header; detect the IPv4 version nibble. Port is 0 for non-TCP/UDP. Pure
    so it can be unit-tested with synthetic bytes.
    """
    off = 0
    if len(payload) > 14 and (payload[0] >> 4) != 4 and (payload[14] >> 4) == 4:
        off = 14  # Ethernet header
    if off + 20 > len(payload) or (payload[off] >> 4) != 4:
        return "", 0
    ihl = (payload[off] & 0x0F) * 4
    if ihl < 20 or off + ihl > len(payload):
        return "", 0
    proto = payload[off + 9]
    dst = ".".join(str(b) for b in payload[off + 16 : off + 20])
    port = 0
    if proto in (6, 17) and off + ihl + 4 <= len(
        payload
    ):  # TCP / UDP, L4 header present
        port = int.from_bytes(payload[off + ihl + 2 : off + ihl + 4], "big")
    return dst, port


# ---------------------------------------------------------------------------
# Interactive consent: egress-sidecar WS client + hold paths (#2311 half B).
# ---------------------------------------------------------------------------


def _ws_url(consent_url: str) -> str:
    """Derive the ``/ws/egress-sidecar`` WS URL from the consent HTTP URL.

    ``http(s)://host:port/...`` -> ``ws(s)://host:port/ws/egress-sidecar``.
    Reuses :data:`CONSENT_URL`'s scheme+host so no new env var is needed; the
    sidecar leg of the consent contract (``sidecar <-WS-> klangkd``).
    """
    if consent_url.startswith("https://"):
        host = consent_url[len("https://") :].split("/", 1)[0]
        return f"wss://{host}/ws/egress-sidecar"
    if consent_url.startswith("http://"):
        host = consent_url[len("http://") :].split("/", 1)[0]
        return f"ws://{host}/ws/egress-sidecar"
    return consent_url  # already ws(s)://, or an exotic scheme used verbatim


class _HoldLimiter:
    """Bounded in-flight DNS holds (single-threaded asyncio: no lock needed).

    A flood of denied queries past :data:`HOLD_LIMIT` fail-closes to NXDOMAIN
    rather than queuing unbounded hold tasks. ``try_acquire`` / ``release``
    run between await points on the loop thread, so they are atomic.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._in_flight = 0

    def try_acquire(self) -> bool:
        if self._in_flight >= self._limit:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1


class SidecarConsentClient:
    """Persistent WS client to klangkd's ``/ws/egress-sidecar`` (#2311 half B).

    One socket per workspace, opened at startup, reconnected (exponential
    backoff) on drop. :meth:`request` sends an egress frame and awaits the
    matching verdict frame by id. **Fail-close**: a down connection or a
    timed-out verdict resolves to ``"deny"`` immediately, so the proxy
    NXDOMAIN/DROPs (today's static behavior) when klangkd or the decider is
    unreachable -- the workspace never hangs on a pending connection.

    All ``_pending`` state lives on the event-loop thread (coroutines +
    ``_run``'s receive loop); the NFQUEUE consumer reaches the loop via
    ``asyncio.run_coroutine_threadsafe`` (it never touches ``_pending``
    directly).
    """

    def __init__(self, consent_url: str, token_path: str, hold_timeout: float) -> None:
        self._url = _ws_url(consent_url)
        self._token_path = token_path
        self._hold_timeout = hold_timeout
        self._ws = None  # current websockets connection, or None
        self._connected = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}  # id -> Future
        self._stop = False
        self._no_token_warned = False
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._fail_close_pending()

    async def _run(self) -> None:
        import websockets  # sidecar-only dep; lazy so the module loads without it

        backoff = 1.0
        while not self._stop:
            token = self._read_token()
            if not token:
                # Token file not present yet (workspace JWT not written). Retry
                # without escalating backoff (expected at startup).
                if DEBUG and not self._no_token_warned:
                    print(
                        "consent: workspace token not yet present; retrying",
                        flush=True,
                    )
                    self._no_token_warned = True
                await asyncio.sleep(1.0)
                continue
            url = f"{self._url}?token={token}"
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10, close_timeout=5
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._no_token_warned = False
                    backoff = 1.0
                    if DEBUG:
                        print(f"consent: connected to {self._url}", flush=True)
                    async for raw in ws:
                        self._dispatch(raw)
            except Exception as exc:
                # Log the exception TYPE only -- the URL carries the workspace
                # JWT as ?token=, and a websockets exception could embed it
                # (#2309). The websockets package logger is capped at WARNING
                # on import so its DEBUG request-line can't leak either.
                if DEBUG:
                    print(
                        f"consent: connection error: {type(exc).__name__}",
                        flush=True,
                    )
            finally:
                # Disconnected (clean close, error, or crash): the sidecar's
                # held connections die with it (fail-close). Any in-flight
                # request resolves to deny so its caller NXDOMAIN/DROPs.
                self._ws = None
                self._connected.clear()
                self._fail_close_pending()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)  # capped exponential backoff

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict) or msg.get("type") != "verdict":
            return
        vid = msg.get("id")
        decision = msg.get("decision")
        if not isinstance(vid, str):
            return
        fut = self._pending.pop(vid, None)
        if fut is not None and not fut.done():
            # Any non-"allow" verdict (deny, expired, malformed) -> deny.
            fut.set_result(decision if decision == "allow" else "deny")

    async def request(self, dst: str, dport: int | None) -> str:
        """Send an egress frame + await the verdict. Fail-close -> ``"deny"``.

        Returns ``"allow"`` or ``"deny"``. A down connection returns ``"deny"``
        at once (no frame sent); a timed-out or disconnected-in-flight request
        resolves to ``"deny"`` so the caller fail-closes.
        """
        if not self._connected.is_set() or self._ws is None:
            return "deny"
        lid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[lid] = fut
        frame = json.dumps({"type": "egress", "id": lid, "dst": dst, "dport": dport})
        try:
            await self._ws.send(frame)
        except Exception:
            self._pending.pop(lid, None)
            return "deny"
        try:
            return await asyncio.wait_for(fut, self._hold_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(lid, None)
            return "deny"

    def _fail_close_pending(self) -> None:
        for lid in list(self._pending):
            fut = self._pending.pop(lid, None)
            if fut is not None and not fut.done():
                fut.set_result("deny")

    def _read_token(self) -> str:
        try:
            with open(self._token_path) as f:
                return f.read().strip()
        except OSError:
            return ""


async def _forward_and_learn(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    qname: str,
    port_set: set[int | None],
) -> None:
    """Forward a query wire to the upstream + learn/respond (allow path).

    Shared by the static-allow path and the consent-allow path (which passes
    ``{None}`` -- all-ports, like a port-less allow spec). Uses a non-blocking
    socket + the loop's sock_* helpers so the await yields to other holds +
    the WS receive loop while the upstream is pending.
    """
    loop = asyncio.get_running_loop()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
    us.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_sendto(us, data, UPSTREAM), 3)
        resp, _ = await asyncio.wait_for(loop.sock_recvfrom(us, 65535), 3)
    except Exception:
        return
    finally:
        us.close()  # closed on success, error, AND cancellation
    await _respond_allowed(s, resp, addr, qname, port_set)


def _send_nxdomain(s: socket.socket, data: bytes, addr: tuple[str, int]) -> None:
    try:
        s.sendto(nxdomain_for(data), addr)
    except Exception:
        pass


def _gate_deny(client: SidecarConsentClient | None, limiter: _HoldLimiter) -> bool:
    """True if a denied query should be held (ask the coordinator); False if it
    should fail-close NXDOMAIN inline -- no client, WS down, or the flood bound
    is exhausted. On True the limiter slot is acquired (released by the hold).
    Pure + inline so a flood of fail-close denies never spawns a task per query
    (only real holds do, bounded by :data:`HOLD_LIMIT`).
    """
    if client is None or not client.connected:
        return False
    return limiter.try_acquire()


async def _handle_hold(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    qname: str,
    client: SidecarConsentClient,
    limiter: _HoldLimiter,
) -> None:
    """Hold a denied DNS query pending the consent verdict (#2311 half B).

    The caller has already gate-checked + acquired the limiter slot (see
    :func:`_gate_deny`): ``allow`` resolves upstream + learns the IPs
    (all-ports, TTL-tracked); ``deny``/timeout -> NXDOMAIN. Runs as a task so
    the hold never blocks the receive loop.
    """
    try:
        decision = await client.request(qname, None)
    except Exception:
        decision = "deny"
    finally:
        limiter.release()
    if decision == "allow":
        if DEBUG:
            print(f"consent-allow {qname}", flush=True)
        # all-ports {None}: a human consenting to a domain opens every port to
        # its resolved IPs for the TTL (like a port-less allow spec) -- consent
        # is per-domain, not per-port (#2311 half B).
        await _forward_and_learn(s, data, addr, qname, {None})
    else:
        if DEBUG:
            print(f"consent-deny  {qname}", flush=True)
        _send_nxdomain(s, data, addr)


def _run_nfq_consumer(
    client: SidecarConsentClient | None, loop: asyncio.AbstractEventLoop
) -> None:
    """Bind the sidecar's NFQUEUE and consent-gate blocked IP egress (#2311).

    Runs ``nfq.run()`` in a thread (netfilterqueue is synchronous). The
    callback holds the packet pending the verdict by crossing into the loop
    via ``run_coroutine_threadsafe`` (blocking): ``allow`` -> ``pkt.accept()``
    (conntrack's ``ESTABLISHED,RELATED`` rule passes the rest of the
    connection); ``deny``/timeout/WS-down -> ``pkt.drop()`` (fail-close).
    Blocking the callback serializes NFQUEUE; the kernel queue length + the
    iptables rate-limit in entrypoint.sh absorb bursts (overflows DROP).
    netfilterqueue is a sidecar-only dep, imported lazily so the module loads
    without it.
    """
    try:
        from netfilterqueue import NetfilterQueue
    except Exception as exc:
        print(f"nfqueue: netfilterqueue unavailable ({exc})", flush=True)
        return

    def _cb(pkt) -> None:
        dst, port = parse_dest(pkt.get_payload())
        if not dst or client is None or not client.connected:
            pkt.drop()  # unparseable / no consent / WS down -> fail-close
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(client.request(dst, port), loop)
            decision = fut.result(HOLD_TIMEOUT)
        except Exception:
            decision = "deny"  # timeout / loop dead -> fail-close
        if decision == "allow":
            pkt.accept()
        else:
            pkt.drop()

    try:
        nfq = NetfilterQueue()
        nfq.bind(QUEUE_NUM, _cb)
        print(f"nfqueue consumer bound to queue {QUEUE_NUM}", flush=True)
        nfq.run()
    except Exception as exc:
        print(f"nfqueue consumer failed: {exc}", flush=True)


async def _handle_packet(
    s: socket.socket,
    data: bytes,
    addr: tuple[str, int],
    client: SidecarConsentClient | None,
    limiter: _HoldLimiter,
) -> None:
    """Classify + route one DNS query (the per-packet body of :func:`_async_main`).

    Factored out of the receive loop so the routing -- classify -> gate ->
    hold / NXDOMAIN / forward -- is unit-testable without running the infinite
    ``recvfrom`` loop. A statically-allow-listed name goes straight to the
    forward path (never held); only denied names hit the consent gate.
    """
    try:
        qname = query_name(data)
    except Exception:
        return  # malformed/unparseable query -> drop
    ports = ports_for(qname)
    deny, port_set = _decision(qname, ports)
    if deny:
        if DEBUG:
            print(f"deny  {qname}", flush=True)
        if _gate_deny(client, limiter):
            t = asyncio.create_task(_handle_hold(s, data, addr, qname, client, limiter))
            _BG_TASKS.add(t)  # strong ref so the hold task isn't GC'd
            t.add_done_callback(_BG_TASKS.discard)
        else:
            _send_nxdomain(s, data, addr)
        return
    await _forward_and_learn(s, data, addr, qname, port_set)


async def _async_main() -> None:
    """The asyncio DNS loop (#2311 half B): holds denied queries pending verdicts.

    Allowed queries forward inline (serial, like the pre-consent proxy); denied
    queries run as bounded tasks so a hold never blocks the receive loop.
    """
    loop = asyncio.get_running_loop()
    client: SidecarConsentClient | None = None
    if CONSENT_URL:
        client = SidecarConsentClient(CONSENT_URL, WORKSPACE_TOKEN_PATH, HOLD_TIMEOUT)
        await client.start()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", LISTEN_PORT))
    s.setblocking(False)
    print(
        f"dns-proxy listening on 127.0.0.1:{LISTEN_PORT} "
        f"(upstream={UPSTREAM[0]}, allowed={SPECS})",
        flush=True,
    )
    check_mark()
    _sweep = asyncio.create_task(_async_sweeper())
    _BG_TASKS.add(_sweep)  # strong ref for the loop's lifetime
    _sweep.add_done_callback(_BG_TASKS.discard)
    # NFQUEUE consumer runs nfq.run() in a thread (blocking); its callback
    # crosses into this loop to await the consent verdict (#2311 half B).
    if CONSENT_URL:
        threading.Thread(
            target=_run_nfq_consumer, args=(client, loop), daemon=True
        ).start()
    limiter = _HoldLimiter(HOLD_LIMIT)
    while True:
        try:
            data, addr = await loop.sock_recvfrom(s, 65535)
        except Exception:
            continue
        await _handle_packet(s, data, addr, client, limiter)


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
