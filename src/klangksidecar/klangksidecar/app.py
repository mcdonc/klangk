"""Asyncio orchestration: the DNS recv loop + SIGTERM teardown + main() (#2450).

async_main binds the DNS socket, starts the consent client + TTL sweeper +
NFQUEUE consumer, and runs the per-query loop; shutdown tears it all down on
SIGTERM (#2400); main is the PID-1 entry.
"""

from __future__ import annotations

import asyncio
import signal
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import allowlist, config, consent, nfqueue, packets, resolve, rules
from .config import DEBUG, HOLD_TIMEOUT, LISTEN_PORT, UPSTREAM, WORKSPACE_TOKEN_PATH
from .state import BG_TASKS

if TYPE_CHECKING:
    from .consent import SidecarConsentClient

# Bound on the consent client's teardown during SIGTERM shutdown (#2400):
# client.stop() closes the WS (close_timeout=5s), and during klangkd shutdown
# the server may be going away, so an unbounded close handshake could
# re-introduce the 5s window this fix eliminates. Bounded so the whole
# teardown fits well inside podman's `stop -t 5`.
SHUTDOWN_CLIENT_TIMEOUT = 2.0


async def cancel_task(task: asyncio.Task | None) -> None:
    """Cancel a background task and swallow its end (best-effort teardown;
    process exit reaps whatever remains)."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def unbind_nfq(nfq) -> None:
    """Remove the NFQUEUE reader and unbind (each best-effort)."""
    if nfq is None:
        return
    try:
        asyncio.get_running_loop().remove_reader(nfq.get_fd())
    except Exception:
        pass
    try:
        nfq.unbind()
    except Exception:
        pass


def close_quietly(sock: socket.socket) -> None:
    """Close a socket, swallowing errors."""
    try:
        sock.close()
    except Exception:
        pass


async def shutdown(
    client: SidecarConsentClient | None,
    nfq,
    sock: socket.socket,
    sweep: asyncio.Task | None,
    sampler: asyncio.Task | None = None,
) -> None:
    """Clean teardown on SIGTERM (#2400): stop the consent client, cancel the
    TTL sweeper, unbind NFQUEUE, close the DNS socket.

    The consent client's stop is bounded (:data:`SHUTDOWN_CLIENT_TIMEOUT`) so a
    stalled WebSocket close handshake can't re-introduce the 5s window. Best-
    effort — a failure (or the bound) in one step must not skip the rest
    (process exit reaps whatever remains). Runs in :func:`async_main`'s
    ``finally`` after the SIGTERM handler cancels the main task, so the proxy
    exits promptly instead of relying on podman's SIGKILL fallback — which a
    PID-1 sidecar always hit, because the kernel ignores default SIGTERM
    dispositions for a PID-namespace init (SIGNAL_UNKILLABLE: a fatal signal
    with no explicit handler is skipped for init).
    """
    if client is not None:
        try:
            await asyncio.wait_for(client.stop(), SHUTDOWN_CLIENT_TIMEOUT)
        except (asyncio.CancelledError, Exception):
            # CancelledError widened in (#2657): stop() awaiting its cancelled
            # run task raises it through the wait_for, `except Exception`
            # doesn't catch a BaseException (3.8+), and the escape aborted the
            # rest of teardown -- the exact failure mode the sweep/sampler
            # guards below already widen against.
            pass
    await cancel_task(sweep)
    await cancel_task(sampler)
    unbind_nfq(nfq)
    close_quietly(sock)


def resolve_ws_host(consent_url: str) -> str | None:
    """Resolve the klangkd WS host to an IP so the egress-accounting rule can
    exclude it (#2485) -- the WS is the sidecar's own persistent control socket
    and its keepalives must not self-sustain the idle timer.

    Returns None on any failure (no host in the URL, or unresolvable). IMPORTANT:
    a None DEFEATS the idle timeout -- the WS keepalive (~50-100 bytes per 20s
    ping) then counts as workspace egress and bumps the timer every tick, so the
    workspace is never reaped. In the default deployment CONSENT_URL is
    host.containers.internal (a /etc/hosts entry, single IP) and resolves
    locally, so this is a round-robin / DNS-only-backend edge case; it is logged
    so an operator whose workspaces never idle can find it.
    """
    try:
        host = urlparse(consent_url).hostname
        ip = socket.gethostbyname(host) if host else None
    except Exception:
        ip = None
    if not ip:
        print(
            "egress-sidecar: could not resolve the klangkd WS host to exclude "
            "it from egress accounting -- its keepalives will count as workspace "
            "egress and the workspace idle timer may never fire (#2485)",
            flush=True,
        )
    return ip


async def start_consent_client() -> SidecarConsentClient | None:
    """Start the WS consent client when consent is configured (a
    :data:`config.CONSENT_URL`), else None (static mode)."""
    if not config.CONSENT_URL:
        return None
    client = consent.SidecarConsentClient(
        config.CONSENT_URL, WORKSPACE_TOKEN_PATH, HOLD_TIMEOUT
    )
    await client.start()
    return client


def bind_dns_socket() -> socket.socket:
    """Bind the non-blocking UDP DNS socket the per-query loop serves on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", LISTEN_PORT))
    s.setblocking(False)
    print(
        f"dns-proxy listening on 127.0.0.1:{LISTEN_PORT} "
        f"(upstream={UPSTREAM[0]}, allowed={allowlist.SPECS})",
        flush=True,
    )
    return s


async def start_consent_services(loop, client: SidecarConsentClient) -> tuple:
    """Start the NFQUEUE consumer + the egress sampler (interactive mode).

    #2485: the egress byte-accounting rule + the sampler that bumps the idle
    timer on real workspace traffic (long-lived / UDP flows the #2481 DNS+SYN
    hooks miss). Best-effort; a missing rule -> flat zero counter -> sampler
    never bumps (falls back to #2481). resolve_ws_host scopes the rule to
    exclude the sidecar's own WS so its keepalives can't self-sustain the
    timer. Resolve + install run off-loop (gethostbyname + 2 iptables forks
    are blocking), matching the rest of startup. Returns ``(nfq, sampler)``.
    """
    packets.check_rst_socket()  # eager-deny RST forge (#2345); best-effort (NET_RAW)
    nfq = nfqueue.setup_nfq_consumer(client)  # bound NFQUEUE, for shutdown (#2400)
    ws_ip = await loop.run_in_executor(None, resolve_ws_host, config.CONSENT_URL)
    await loop.run_in_executor(None, rules.install_acct, ws_ip)
    sampler = asyncio.create_task(
        consent.activity_sampler(client, rules.acct_bytes, config.ACTIVITY_GATE_S)
    )
    BG_TASKS.add(sampler)
    sampler.add_done_callback(BG_TASKS.discard)
    return nfq, sampler


async def serve_dns(s: socket.socket, client: SidecarConsentClient | None) -> None:
    """The per-query recv loop; exits only via the SIGTERM CancelledError,
    never by falling through the condition -- the arc to loop exit is
    unreachable."""
    loop = asyncio.get_running_loop()
    while True:  # pragma: no branch
        try:
            data, addr = await loop.sock_recvfrom(s, 65535)
        except Exception:
            continue
        await resolve.handle_packet(s, data, addr, client)


async def async_main() -> None:
    """The asyncio DNS loop (#2311 half B, #2324): allow-listed + denied names
    resolve inline; a denied name in interactive mode records IP->host so its
    connection SYN is consent-gated at NFQUEUE (a separate thread).

    Installs an explicit SIGTERM handler (#2400) so podman's ``stop`` signal
    triggers a clean, prompt teardown instead of being ignored (the sidecar is
    PID 1, and the kernel suppresses default terminate dispositions for init).
    """
    loop = asyncio.get_running_loop()
    client = await start_consent_client()
    s = bind_dns_socket()
    rules.check_mark()
    _sweep = asyncio.create_task(rules.async_sweeper())
    BG_TASKS.add(_sweep)  # strong ref for the loop's lifetime
    _sweep.add_done_callback(BG_TASKS.discard)
    # NFQUEUE consumer is driven by this event loop (get_fd + add_reader) so a
    # slow verdict on one SYN doesn't serialize others (#2324, #2329).
    nfq = None
    _sampler: asyncio.Task | None = None
    if client is not None:
        nfq, _sampler = await start_consent_services(loop, client)
    # The sidecar is PID 1 (entrypoint.sh execs python). The kernel suppresses
    # default terminate/stop dispositions for a PID-namespace init: a SIGTERM
    # with no handler installed is effectively ignored, so podman's `stop -t 5`
    # SIGTERM was no-op'd and EVERY removal fell back to SIGKILL after the full
    # 5s window (occasionally wedging in Stopping). Install an explicit handler
    # that cancels this task -> shutdown closes the WS, unbinds NFQUEUE, closes
    # the socket -> prompt exit (#2400). (SIGKILL/SIGSTOP bypass this and always
    # work, which is why podman's SIGKILL fallback eventually cleared it.)
    main_task = asyncio.current_task()
    stopping = False

    def _on_sigterm() -> None:
        # Idempotent: a second SIGTERM arriving while shutdown is mid-await
        # must NOT re-cancel the main task -- that CancelledError is a
        # BaseException, so shutdown's `except Exception` guards don't catch
        # it and teardown would be aborted (skipping nfq.unbind/sock.close).
        # The first signal cancels; subsequent ones are no-ops (SIGKILL remains
        # podman's hard backstop if teardown hangs) (#2400).
        nonlocal stopping
        if stopping:
            return
        stopping = True
        # Captured from asyncio.current_task() inside async_main, so never
        # None -- the guard only satisfies the type-checker (a plain loop
        # callback would see None; _on_sigterm is registered from a task).
        if main_task is not None:  # pragma: no branch
            main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        pass  # signal handlers need the main thread + a supported loop backend
    try:
        await serve_dns(s, client)
    except asyncio.CancelledError:
        if DEBUG:
            print("dns-proxy: stop signal received, shutting down", flush=True)
    finally:
        await shutdown(client, nfq, s, _sweep, _sampler)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
