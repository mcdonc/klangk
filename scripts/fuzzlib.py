"""Shared scaffolding for the fuzz harnesses (fuzz-api, fuzz-idle)."""

from __future__ import annotations

import logging
import random

import httpx


def server_start_budget() -> float:
    """Wall clock a fuzz harness allots for klangkd boot.

    ``prewarm_podman()`` runs its first ``podman create`` under
    ``podman.bringup_timeout()`` (120 s local, 240 s on CI, #3064), and
    ``Podman._run_create`` retries once on timeout, so the prewarm worst
    case is two full create budgets; the prewarm teardown (``podman stop``
    then ``rm -f`` via ``remove_container``, 30 s each at the ``run()``
    default) adds up to 60 s more; 30 s covers uvicorn boot, migrations,
    seeding, and the startup reaps. The fixed budgets that preceded this
    (30 s, then 120 s, #3368) sat below the server's own give-up point,
    so a slow-but-legitimate create aborted the fuzz session with 0
    requests sent (#3412).
    """
    # Lazy so --check-style entry points that never start a server need
    # no backend import.
    from klangk.podman import bringup_timeout

    return 30 + 2 * bringup_timeout() + 60


def configure_logging() -> None:
    """The fuzz-log posture: INFO root, per-request httpx noise suppressed."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def draw_seed(raw: int | None) -> int:
    """The run's RNG seed: the --seed value, else a fresh draw."""
    return raw if raw is not None else random.randint(0, 2**32)


def uds_login(uds_path: str, identifier: str, password: str) -> str:
    """Log in over the backend UDS and return the access token.

    The login body's field is ``identifier`` (email or handle, #616) —
    posting the legacy ``email`` key 422s and the whole fuzz run
    silently sends nothing.
    """
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=uds_path),
        base_url="http://klangkd",
        timeout=10,
    ) as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"identifier": identifier, "password": password},
        )
        r.raise_for_status()
        return r.json()["access_token"]
