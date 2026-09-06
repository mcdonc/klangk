"""Built-in audit-record forwarding to a syslog/SIEM target (#3252).

``KLANGKD_AUDIT_FORWARD_URL`` (an HTTPS JSON endpoint) and/or
``KLANGKD_AUDIT_FORWARD_SYSLOG`` (an RFC 5424 TCP or TLS receiver)
arm an opt-in forwarder that ships every **new row** of the three
audit tables — ``audit_events``, ``container_events``,
``egress_consent`` — to the configured target, so the records reach
a different system / centralized log repository without a host-side
shipper (STIG SV-222481/482). The log-stream path (#3156, JSON logs +
rsyslog ``imfile``/fluent-bit) keeps working alongside this one; the
forwarder carries the **records** (structured detail, actor, target),
not the log lines.

Delivery semantics — at-least-once, per-table order:

- The cursor is one row per source table in ``audit_forward_state``
  (see :mod:`klangk.model.audit_forward`); a row ships when its
  table's cursor passes it, in insert order.
- A batch is delivered, then the cursor advances past it. A crash
  between the two replays that batch — duplicates are possible, gaps
  are not. The batch size bounds the in-memory queue; the tables
  themselves are the durable backlog while a target is down.
- A failed delivery backs off exponentially (5s doubling, capped at
  300s), the cursor stays put, and the failure is surfaced on
  ``/audit`` (``forwarding.healthy`` / ``forwarding.last_error``) —
  the assessor-visible signal that records are not reaching the
  target. Retention pruning (``audit_events_retention_days`` etc.)
  still applies while delivery lags, so a target down longer than a
  retention window loses the oldest unforwarded rows — keep the
  window comfortably larger than any expected outage.

The HTTP target shares transport plumbing with the #3250 webhook
channel (:func:`klangk.webhook.post_json`); the syslog target speaks
RFC 5424 (facility ``audit``, severity ``info``, one line per record,
newline-framed per the common TCP convention, the full record as the
JSON message).

The worker follows the :class:`~klangk.interval.IntervalWorker`
shape: sweep on a short interval, settings read live per sweep (a
SIGHUP reload changes targets on the next pass), failures logged and
retried — forwarding is housekeeping, never a correctness path for
the audited action itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from klangk.interval import IntervalWorker
from klangk.model.audit_forward import SOURCE_ORDER
from klangk.webhook import post_json

logger = logging.getLogger(__name__)

# Sweep cadence: how often each source is polled for new rows. The
# sweep also doubles as the delivery-retry interval once backoff
# allows an attempt.
FORWARD_INTERVAL_SECONDS = 5.0

# Rows read (and held in memory) per source per sweep — the bounded
# local queue. A larger backlog drains over successive sweeps.
FORWARD_BATCH_LIMIT = 500

# Exponential retry: 5s, 10s, 20s ... capped here.
FORWARD_MAX_BACKOFF_SECONDS = 300.0

# One POST per batch; how long to wait for connect + response.
FORWARD_HTTP_TIMEOUT_SECONDS = 10.0

# Connect + write + drain budget for one syslog delivery attempt —
# the same order of bound as the HTTP path, so a hanging receiver
# (accepted connection, never reads) cannot wedge the sweep forever.
FORWARD_SYSLOG_TIMEOUT_SECONDS = 10.0

# RFC 5424 syslog constants: facility 13 is "audit", severity 6 is
# "informational" — every forwarded record is a completed fact.
SYSLOG_FACILITY_AUDIT = 13
SYSLOG_SEVERITY_INFO = 6

VALID_SYSLOG_SCHEMES = ("tcp", "tls")
DEFAULT_SYSLOG_PORTS = {"tcp": 514, "tls": 6514}

# RFC 9110 token chars, restricted to the readable set (header names
# sent by the forwarder are simple words like Authorization).
HEADER_NAME_OK = re.compile(r"^[A-Za-z0-9-]+$")


def sanitize_hostname(name: str) -> str:
    """RFC 5424 HOSTNAME: printable ASCII (0x21-0x7e), no spaces,
    1-255 chars."""
    cleaned = "".join(
        ch if 33 <= ord(ch) <= 126 else "-" for ch in name.strip()
    )
    return cleaned[:255] or "-"


# Stable per process (the sender identity in every forwarded line).
SYSLOG_HOSTNAME = sanitize_hostname(socket.gethostname())


def validate_forward_url(value: str) -> None:
    """Raise ``ValueError`` unless *value* is an http(s) URL.

    Startup-time validation for ``KLANGKD_AUDIT_FORWARD_URL`` — a
    malformed target fails loudly instead of failing on every sweep.
    ``http://`` is accepted for loopback collectors; use ``https://``
    for any target off the host (audit records name users and
    actions).
    """
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            f"audit forward URL {value!r} must be an http:// or"
            " https:// URL with a host"
        )


def validate_forward_header(value: str) -> None:
    """Raise ``ValueError`` unless *value* is one ``Name: value``
    header with a valid token name (RFC 9110 token characters).

    The one-header shape covers the collector-auth case (Splunk HEC's
    ``Authorization: Splunk <token>``, a bearer token) without a
    header-map surface.
    """
    name, sep, header_value = value.partition(":")
    if not sep or not header_value.strip():
        raise ValueError(
            f"audit forward header {value!r} must be a single"
            " 'Name: value' header"
        )
    if not HEADER_NAME_OK.match(name):
        raise ValueError(
            f"audit forward header name {name!r} is not a valid"
            " header name (letters, digits, and hyphens)"
        )


def parse_syslog_target(value: str) -> tuple[str, str, int]:
    """Parse ``KLANGKD_AUDIT_FORWARD_SYSLOG`` into (scheme, host, port).

    Accepts ``tcp://host[:port]`` and ``tls://host[:port]`` (a bare
    ``host[:port]`` defaults to tcp). The port defaults to 514 (tcp)
    / 6514 (tls). Anything else raises ``ValueError`` — validated at
    startup, same fail-loud posture as the URL target.
    """
    raw = value if "://" in value else f"tcp://{value}"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in VALID_SYSLOG_SCHEMES:
        raise ValueError(
            f"audit forward syslog target {value!r} must use tcp://"
            f" or tls:// (got {scheme!r})"
        )
    if not parsed.hostname:
        raise ValueError(f"audit forward syslog target {value!r} has no host")
    return scheme, parsed.hostname, parsed.port or DEFAULT_SYSLOG_PORTS[scheme]


def ssl_context_for(scheme: str) -> ssl.SSLContext | None:
    """TLS context for the syslog transport; None for plain TCP.

    Default verification (system roots, hostname check) — the SIEM's
    certificate must chain to a CA this host trusts (add it to the
    approved CA baseline, ``KLANGKD_TRUSTED_CA_DIR``, if private).
    """
    return ssl.create_default_context() if scheme == "tls" else None


def rfc3339_utc(epoch: float) -> str:
    """RFC 5424 TIMESTAMP: RFC 3339 with a ``Z`` zone marker."""
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def record_timestamp(record: dict) -> float:
    """The record's event time — ``created_at`` for the event tables,
    ``requested_at`` for consent rows."""
    if "created_at" in record:
        return record["created_at"]
    return record.get("requested_at", 0.0)


def escape_sd_value(value) -> str:
    """RFC 5424 STRUCTURED-DATA PARAM-VALUE escaping (\\, ", ])."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("]", "\\]")
    )


def format_syslog_line(record: dict) -> str:
    """One RFC 5424 line for one forwarded record.

    Header: facility ``audit`` / severity ``info``, the record's own
    event time, this host, ``klangkd``, the pid, and the source table
    as MSGID. STRUCTURED-DATA carries the source + cursor; MSG is the
    full record as JSON.
    """
    pri = SYSLOG_FACILITY_AUDIT * 8 + SYSLOG_SEVERITY_INFO
    structured = (
        f'[klangk source="{escape_sd_value(record["source"])}"'
        f' cursor="{escape_sd_value(record["forward_cursor"])}"]'
    )
    message = json.dumps(record, separators=(",", ":"), sort_keys=True)
    return (
        f"<{pri}>1 {rfc3339_utc(record_timestamp(record))}"
        f" {SYSLOG_HOSTNAME} klangkd {os.getpid()}"
        f" {record['source']} {structured} {message}"
    )


async def close_writer_quietly(writer) -> None:
    """Close a stream writer; secondary close errors are swallowed so
    they cannot mask the delivery error that triggered the close."""
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


class AuditForwarder(IntervalWorker):
    """Ships new rows of the three audit tables to the configured
    target(s) (#3252). Inert until a target is configured — the
    unconfigured sweep reads nothing and sends nothing.

    Owns only ``app`` (the state-object ownership rule): every
    setting is read live per sweep, so a SIGHUP reload swaps targets
    on the next pass.
    """

    log_label = "audit forwarder"

    @property
    def interval(self) -> float:
        return FORWARD_INTERVAL_SECONDS

    def __init__(self, app) -> None:
        super().__init__(app)
        self.target_failures: dict[str, int] = {}
        self.target_cooldown_until: dict[str, float] = {}
        self.healthy = True
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None
        self.last_error: str | None = None

    def reconfigure(self, app) -> None:
        """Swap the app reference (SIGHUP reload); retry state resets
        so the new targets get a fresh backoff budget."""
        super().reconfigure(app)
        self.target_failures = {}
        self.target_cooldown_until = {}
        self.healthy = True
        self.last_error = None

    # --- settings (read live) ---

    def targets(self) -> list[tuple[str, str]]:
        """Configured (kind, target) pairs; empty when forwarding is
        off. Both targets may be configured — each receives every
        record."""
        settings = self.app.state.settings
        configured = []
        if settings.audit_forward_url:
            configured.append(("url", settings.audit_forward_url))
        if settings.audit_forward_syslog:
            configured.append(("syslog", settings.audit_forward_syslog))
        return configured

    def url_headers(self) -> dict | None:
        """The optional auth header for the URL target
        (``KLANGKD_AUDIT_FORWARD_HEADER``), parsed fresh from settings
        so a reload applies on the next sweep."""
        raw = self.app.state.settings.audit_forward_header
        if not raw:
            return None
        name, _, value = raw.partition(":")
        return {name.strip(): value.strip()}

    # --- sweep ---

    async def sweep(self) -> None:
        """One forwarding pass over every (source, target) pair not in
        its own backoff cooldown (unconfigured: nothing read, nothing
        sent).

        A delivery failure cools down only the failing target — one
        dead target never delays delivery to the others — and leaves
        its cursor untouched (its rows stay queued for the retry).
        """
        targets = self.active_targets()
        if not targets:
            return
        try:
            await self.forward_all(targets)
        except Exception as exc:  # noqa: BLE001 — retried next sweep
            self.note_global_failure(exc)
            return
        await self.note_healthy_sweep()

    def active_targets(self) -> list[tuple[str, str]]:
        """Configured targets not currently in a backoff cooldown."""
        now = time.monotonic()
        return [
            target
            for target in self.targets()
            if now >= self.target_cooldown_until.get(target[1], 0.0)
        ]

    async def note_healthy_sweep(self) -> None:
        """A sweep that raised nowhere delivered everything every target
        was due — clear the failure state (``healthy: false`` with an
        empty queue would confuse an assessor reading ``/audit``; every
        failure path raises, so no-exception means success)."""
        self.record_success()

    async def forward_all(self, targets: list[tuple[str, str]]) -> None:
        """Forward one batch from every source to every target. Every
        pair is attempted — each failure cools down its own target and
        is re-raised at the end (one sweep-level failure record)."""
        failures: list[BaseException] = []
        for source in SOURCE_ORDER:
            await self.forward_source(source, targets, failures)
        if failures:
            raise failures[0]

    async def forward_source(
        self,
        source: str,
        targets: list[tuple[str, str]],
        failures: list,
    ) -> None:
        """One source to every active target; a target that failed
        earlier this sweep is skipped."""
        for target in targets:
            if self.in_cooldown(target):
                continue
            try:
                await self.forward_target(source, target)
            except Exception as exc:  # noqa: BLE001 — keep going
                self.record_failure(target, exc)
                failures.append(exc)

    def in_cooldown(self, target: tuple[str, str]) -> bool:
        """True while *target*'s failure backoff has not elapsed."""
        return time.monotonic() < self.target_cooldown_until.get(
            target[1], 0.0
        )

    def state_key(self, source: str, target: tuple[str, str]) -> str:
        """The per-(source, target) watermark key. The target string is
        hashed, not embedded raw (a URL can carry credentials)."""
        digest = hashlib.sha256(target[1].encode()).hexdigest()[:16]
        return f"{source}:{digest}"

    async def forward_target(
        self, source: str, target: tuple[str, str]
    ) -> int:
        """Ship this source's rows past this target's cursor, then
        advance that cursor. A re-configured target hashes to a new
        key and starts from zero — it receives the retained backlog
        (at-least-once permits the replay)."""
        forward_model = self.app.state.model.audit_forward
        key = self.state_key(source, target)
        cursor = await forward_model.watermark(key)
        rows = await forward_model.rows_after(
            source, cursor, FORWARD_BATCH_LIMIT
        )
        if not rows:
            return
        records = [dict(row, source=source) for row in rows]
        await self.deliver(target, records)
        await forward_model.advance(key, rows[-1]["forward_cursor"])
        self.target_failures.pop(target[1], None)

    async def deliver(
        self, target: tuple[str, str], records: list[dict]
    ) -> None:
        """Send one batch to one target; failures raise to the caller
        (the batch stays queued — every target is at-least-once,
        duplicates permitted)."""
        kind, value = target
        if kind == "url":
            await post_json(
                value,
                {"records": records},
                timeout=FORWARD_HTTP_TIMEOUT_SECONDS,
                headers=self.url_headers(),
            )
        else:
            await self.send_syslog(value, records)

    async def send_syslog(self, target: str, records: list[dict]) -> None:
        """Deliver one batch as newline-framed RFC 5424 lines over one
        connection per attempt, bounded by
        :data:`FORWARD_SYSLOG_TIMEOUT_SECONDS` so a hung receiver
        (accepted connection, never reads) cannot wedge the sweep."""
        scheme, host, port = parse_syslog_target(target)
        payload = "".join(
            format_syslog_line(record) + "\n" for record in records
        )
        await asyncio.wait_for(
            self.write_syslog(host, port, ssl_context_for(scheme), payload),
            FORWARD_SYSLOG_TIMEOUT_SECONDS,
        )

    async def write_syslog(
        self, host: str, port: int, ssl_ctx, payload: str
    ) -> None:
        """Open, write, close — the part ``send_syslog`` bounds with a
        timeout.

        On cancellation (the timeout fired) the transport is aborted,
        not gracefully closed: ``close()`` would flush the unsent
        buffer to a receiver that never reads, and ``wait_for`` cannot
        finish until this task exits — so the graceful path would hang
        the timeout itself. ``abort()`` discards the buffer and severs
        the connection immediately.
        """
        _, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
        try:
            writer.write(payload.encode())
            await writer.drain()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        finally:
            await close_writer_quietly(writer)

    # --- failure / status bookkeeping ---

    def backoff_seconds(self, target: tuple[str, str]) -> float:
        """This target's retry delay: one interval after its first
        failure, doubling per consecutive failure, capped."""
        failures = self.target_failures.get(target[1], 0)
        doubling = max(failures - 1, 0)
        return min(
            FORWARD_MAX_BACKOFF_SECONDS,
            FORWARD_INTERVAL_SECONDS * 2**doubling,
        )

    def record_failure(self, target: tuple[str, str], exc: Exception) -> None:
        """Note one failed delivery: the target's backoff cooldown and
        the global /audit failure state."""
        key = target[1]
        failures = self.target_failures.get(key, 0) + 1
        self.target_failures[key] = failures
        self.note_global_failure(exc)
        self.target_cooldown_until[key] = (
            time.monotonic() + self.backoff_seconds(target)
        )
        logger.warning(
            "audit forwarding to %s target failed (backoff %.0fs): %s",
            target[0],
            self.backoff_seconds(target),
            exc,
            exc_info=True,
        )

    def note_global_failure(self, exc: Exception) -> None:
        """The sweep-level failure surface (``/audit`` healthy/last_error)."""
        self.healthy = False
        self.last_failure_at = time.time()
        self.last_error = type(exc).__name__

    def record_success(self) -> None:
        """Note a sweep that raised nowhere: clear the global failure
        state (per-target cooldowns elapse on their own; a target's
        counter resets when it next accepts a batch)."""
        self.healthy = True
        self.last_success_at = time.time()
        self.last_error = None

    async def status(self) -> dict:
        """The ``/audit`` view. Unconfigured: ``{"enabled": False}``
        (behavior identical to before the feature existed)."""
        if not self.targets():
            return {"enabled": False}
        return {
            "enabled": True,
            "healthy": self.healthy,
            "targets": sorted(kind for kind, _ in self.targets()),
            "pending": await self.pending_counts(),
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
        }

    async def pending_counts(self) -> dict:
        """Unsent rows per source — the queue depth behind the slowest
        configured target. A source whose count cannot be read reports
        ``None`` (the status surface must not 500 ``/audit``)."""
        counts = {}
        for source in SOURCE_ORDER:
            try:
                counts[source] = await self.pending_count(source)
            except Exception:  # noqa: BLE001 — status surface only
                counts[source] = None
        return counts

    async def pending_count(self, source: str) -> int:
        """Rows past the slowest target's cursor for *source* (a
        target with no watermark yet counts as zero — it is due a
        replay)."""
        forward_model = self.app.state.model.audit_forward
        cursors = [
            await forward_model.watermark(self.state_key(source, target))
            for target in self.targets()
        ]
        return await forward_model.pending_count(
            source, min(cursors, default=0)
        )
