"""SA/ISSO admin notifications for security-relevant events (#3250).

Dual-channel fan-out (email + webhook) fired from the account
lifecycle mutation sites, the audit write-failure paths, and the
container admission memory gate. STIG driver: ASD STIG V6R4 —
SV-222417/18/19/20/22 (SA/ISSO notification on account lifecycle),
SV-222484/485 (alert on audit processing failure), SV-222668 (alert
on low resource conditions).

Semantics (mirroring the workspace-created hook, #2762):
fire-and-forget and best-effort — a notification failure is logged
and never fails the action it annotates. Delivery runs in a detached
background task; :meth:`AdminNotifier.notify_admins` never raises.

Channels: ``admin_notification_emails`` delivers through the existing
:class:`~klangk.emailsvc.EmailService` transport (SMTP or sendmail —
the same one the auth emails use); ``admin_notification_webhook_url``
delivers one JSON POST with a short timeout and no retries. With
neither configured the notifier is inert.

``admin_notify_events`` is the allowlist: an event not on the list
never notifies. Persistent-condition events (``audit.failure``,
``resource.low``) are additionally throttled to one notification per
event name per window — a degraded audit table or a full host must
not mail-bomb the recipients on every occurrence.

The event names mirror the identity audit stream (``user.create``,
``user.update``, …, #3205) so an operator can correlate a
notification with its ``audit_events`` row. ``user.disable`` /
``user.enable`` / ``audit.failure`` / ``resource.low`` are
notifier-only names (the audit stream records disable toggles under
``user.update``).
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Every event name a notification site can emit. Also the validation
# set for KLANGKD_ADMIN_NOTIFY_EVENTS (a typo there aborts startup —
# the netfilter_default_domains posture: a silently mistyped event
# name would silently disable a security notification).
DEFAULT_NOTIFY_EVENTS = (
    "user.create",
    "user.register",
    "user.update",
    "user.delete",
    "user.unlock",
    "user.disable",
    "user.enable",
    "user.password.change",
    "user.email.change",
    "user.handle.change",
    "group.member.add",
    "group.member.remove",
    "audit.failure",
    "resource.low",
)

# Persistent conditions re-fire at the source on every occurrence (a
# failed audit write, a refused start); notify at most once per event
# name per window so the recipients get one alert, not a storm (#3250).
THROTTLE_SECONDS = {"audit.failure": 300, "resource.low": 300}

# Fire-and-forget tasks are held here so the event loop cannot garbage
# collect a deliver task mid-flight (the asyncio docs' keep-alive set).
PENDING_TASKS: set = set()


def hold_task(task) -> None:
    """Keep a deliver task referenced until it completes."""
    PENDING_TASKS.add(task)
    task.add_done_callback(PENDING_TASKS.discard)


def notify_event(app, event: str, **fields) -> None:
    """Guarded ``notify_admins``: a no-op when *app* has no notifier.

    Production wires ``app.state.notifier`` in ``build_app``; minimal
    test harnesses (and the model layer's unit-test app states) may
    omit it, exactly like the ``getattr(app.state, "hooks", None)``
    guard in lifecycle (#2762). Every emit site goes through here so
    no call path depends on the full state shape.

    Never raises — including from inside the caller's own ``except``
    block (the audit-failure sites): a dispatch problem must not mask
    or replace the exception it annotates.
    """
    try:
        notifier = getattr(app.state, "notifier", None)
        if notifier is not None:
            notifier.notify_admins(event, **fields)
    except Exception:  # noqa: BLE001 — best-effort by contract
        logger.warning(
            "admin notification dispatch for %s failed",
            event,
            exc_info=True,
        )


class AdminNotifier:
    """Owned notification subsystem wired onto ``app.state.notifier``.

    Follows the state-object ownership rule: constructed with
    ``app`` only, every setting read live off
    ``self.app.state.settings`` so a SIGHUP reload applies without
    per-subsystem plumbing.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.last_notified: dict[str, float] = {}

    def reconfigure(self, app) -> None:
        """Swap the app reference (SIGHUP reload); resets throttles."""
        self.app = app
        self.last_notified = {}

    # --- settings (read live) ---

    def recipients(self) -> list[str]:
        """Configured SA/ISSO email recipients (may be empty)."""
        return self.app.state.settings.admin_notification_emails or []

    def webhook_url(self) -> str | None:
        """Configured webhook endpoint, or None when the channel is off."""
        return self.app.state.settings.admin_notification_webhook_url

    def notify_events(self) -> frozenset[str]:
        """The allowlist: every supported event when unset, else the
        operator's explicit list — an empty native list (YAML
        ``admin_notify_events: []``) is notifications-off, while a
        blank env string restores the default allowlist."""
        raw = self.app.state.settings.admin_notify_events
        if raw is None:
            return frozenset(DEFAULT_NOTIFY_EVENTS)
        return frozenset(raw)

    def channels_configured(self) -> bool:
        """True when at least one delivery channel is configured."""
        return bool(self.recipients() or self.webhook_url())

    def throttle_key(self, event: str, detail: dict | None) -> str:
        """One throttle bucket per event — and, when the detail names a
        source ``table``, per table: a container_events write storm must
        not mask the first audit_events degradation alert (and vice
        versa) under the shared 300s window (#3250 review).
        """
        table = (detail or {}).get("table")
        if table is None:
            return event
        return f"{event}:{table}"

    def throttle_allows(self, event: str, key: str) -> bool:
        """Throttle gate: True when *key* may notify now.

        Non-throttled events always pass. A throttled event passes at
        most once per window; passing stamps the clock.
        """
        window = THROTTLE_SECONDS.get(event, 0)
        if window <= 0:
            return True
        now = time.monotonic()
        last = self.last_notified.get(key)
        if last is not None and now - last < window:
            return False
        self.last_notified[key] = now
        return True

    def should_notify(self, event: str, key: str) -> bool:
        """True when *event* is allowlisted, a channel exists, and the
        throttle permits *key*."""
        if event not in self.notify_events():
            return False
        if not self.channels_configured():
            return False
        return self.throttle_allows(event, key)

    # --- entrypoint ---

    def notify_admins(
        self,
        event: str,
        *,
        actor_id: str | None = None,
        actor_email: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict | None = None,
        source_ip: str | None = None,
    ) -> None:
        """Fan one event out to every configured channel. Never raises.

        Fire-and-forget: delivery is a detached background task, so a
        slow SMTP handshake or an unreachable webhook never delays the
        action being notified (which has already succeeded).
        """
        key = self.throttle_key(event, detail)
        if not self.should_notify(event, key):
            return
        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor_id": actor_id,
            "actor_email": actor_email,
            "target_type": target_type,
            "target_id": target_id,
            "detail": detail or {},
            "source_ip": source_ip,
        }
        try:
            hold_task(asyncio.create_task(self.deliver(payload)))
        except RuntimeError:  # no running loop — nowhere to deliver
            logger.warning(
                "admin notification for %s dropped: no running event loop",
                event,
            )

    # --- delivery ---

    async def deliver(self, payload: dict) -> None:
        """Run both channels; each swallows and logs its own failures."""
        await asyncio.gather(
            self.deliver_via_email(payload),
            self.deliver_via_webhook(payload),
        )

    async def deliver_via_email(self, payload: dict) -> None:
        """Send the event to every configured recipient."""
        try:
            recipients = self.recipients()
        except Exception:  # noqa: BLE001 — settings read, best-effort
            logger.warning(
                "admin notification recipients unreadable (event %s)",
                payload["event"],
                exc_info=True,
            )
            return
        for to in recipients:
            subject = f"[klangk] admin event: {payload['event']}"
            try:
                await self.app.state.email.send_plain(
                    to, subject, render_notification_body(payload)
                )
            except Exception:  # noqa: BLE001 — best-effort by design
                logger.warning(
                    "admin notification email to %s failed (event %s)",
                    to,
                    payload["event"],
                    exc_info=True,
                )

    async def deliver_via_webhook(self, payload: dict) -> None:
        """POST the event as JSON; short timeout, no retries."""
        url = self.webhook_url()
        if url is None:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
        except Exception:  # noqa: BLE001 — best-effort by design
            logger.warning(
                "admin notification webhook %s failed (event %s)",
                url,
                payload["event"],
                exc_info=True,
            )


# (field, human label) pairs for the email body — explicit labels,
# not key.title() (which renders "Source Ip").
_BODY_FIELDS = (
    ("timestamp", "Timestamp"),
    ("actor_email", "Actor"),
    ("target_type", "Target type"),
    ("target_id", "Target id"),
    ("source_ip", "Source IP"),
)


def render_notification_body(payload: dict) -> str:
    """Plain-text body for the email channel — one line per field."""
    lines = [f"Event: {payload['event']}"]
    for key, label in _BODY_FIELDS:
        if payload.get(key) is not None:
            lines.append(f"{label}: {payload[key]}")
    if payload.get("detail"):
        lines.append(f"Detail: {payload['detail']}")
    lines.append("")
    lines.append(
        "-- klangkd admin notification (KLANGKD_ADMIN_NOTIFICATION_*)"
    )
    return "\n".join(lines)
