"""Shared JSON-over-HTTP POST plumbing for webhook-style deliveries.

One helper shared by the admin-notification webhook channel (#3250)
and the audit-record forwarder's HTTPS target (#3252): build a
short-lived client, POST one JSON body, raise on a non-2xx response.
Callers own the failure posture — the notifier logs and swallows
(best-effort by contract); the forwarder records the failure and
retries with backoff.
"""

import httpx


async def post_json(
    url: str,
    payload: dict,
    *,
    timeout: float = 5.0,
    headers: dict | None = None,
) -> None:
    """POST *payload* as JSON to *url*; raise on any failure.

    *headers* adds optional request headers (the audit forwarder's
    auth header, #3252). Raises whatever ``httpx`` raises — connection
    errors, timeouts, and ``httpx.HTTPStatusError`` for a non-2xx
    status — so the caller decides how to react (log, retry, surface
    on a status endpoint).
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
