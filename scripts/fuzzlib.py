"""Shared scaffolding for the fuzz harnesses (fuzz-api, fuzz-idle)."""

from __future__ import annotations

import logging
import random

import httpx


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


def uds_login(uds_path: str, email: str, password: str) -> str:
    """Log in over the backend UDS and return the access token."""
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=uds_path),
        base_url="http://klangkd",
        timeout=10,
    ) as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        r.raise_for_status()
        return r.json()["access_token"]
