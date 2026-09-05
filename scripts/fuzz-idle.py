#!/usr/bin/env python3
"""Idle-timeout fuzz harness for klangk (#2514).

Exercises the idle-timeout reaper (``IdleMonitor.cleanup_idle_containers``,
``container.py``) against **real workspaces and real containers**: an own
klangkd is started (UDS, proxy suppressed, real podman bringup), then waves of
workspaces are created with randomized idle-timeout configurations and
activity patterns, each checked against tolerance-based invariants.

Randomized per run (seeded via ``--seed`` for reproducibility):

- Global ``KLANGKD_IDLE_TIMEOUT_SECONDS`` at small values (5-18s), or a
  bogus value (must fall back to the 3600s default with only a warning).
- Per-workspace ``idle_timeout`` overrides (#864/#1018): shorter, longer,
  ``0`` (= never idle out), invalid values (must 4xx, not 5xx).
- Runtime changes mid-flight via the test-mode ``/test/set-idle-timeout``
  endpoint: shorten / lengthen / zero (must wake the cleanup loop).
- Activity patterns, with and without egress (#2479/#2485):
  ``idle`` (no activity at all), ``quiet_ws`` (connected WS, no frames --
  must still be reaped), ``term_under`` / ``term_over`` (terminal bursts
  spaced just under / just over the effective timeout), ``egress``
  (traffic from inside the container via ``podman exec``, bypassing
  klangkd -- the sidecar's flood-gated ``activity`` frame must keep it
  alive), ``mixed``, and ``stops_mid`` (active, then silent).

Invariants (per workspace, all timing tolerant of the reaper's tick
granularity -- reap deadlines are bounded by ``timeout + tick + slack``,
never exact deadlines):

- Idle workspace: container stopped **and removed** (no stopped-but-not-
  removed leaks); the workspace row survives and restarts cleanly.
- Active workspace (any activity source, incl. egress-only): still running
  well past the effective timeout.
- ``idle_timeout=0`` / a lengthening runtime change: not reaped in-window.
- ``/workspaces/{id}/status``'s ``idle_seconds`` tracks wall-clock idle
  within tick tolerance.
- No server-side 5xx and no traceback in the server log at any point.

Usage:
    scripts/fuzz-idle.py [--duration MINUTES] [--seed SEED]
                         [--max-concurrent N] [--waves N] [--log-dir DIR]
                         [--image IMAGE]

Exit code 0 = no anomalies, 1 = any invariant violation / 5xx / traceback.
Requires: podman available, klangk workspace image built
(``devenv shell -- klangk:build-workspace-image``; inside the devenv shell
the image name resolves from the ambient KLANGKD_IMAGE_NAME — no --image
needed).
(``devenv shell -- klangk:build-workspace-image``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field

import httpx

from fuzzlib import configure_logging, draw_seed
import websockets

logger = logging.getLogger("fuzz-idle")

PODMAN = os.environ.get("KLANGKD_PODMAN_BIN", "podman")


def free_port() -> int:
    """A free TCP port (bind(0) + close; races are re-drawn by callers)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- timing bounds (generous; the reaper's tick granularity is the floor) -----

BRINGUP_TIMEOUT = 120  # container running / container_ready after start
RESTART_TIMEOUT = 120  # container running after POST /start on a reaped ws
DELETE_TIMEOUT = 90  # workspace DELETE (stops + removes the container)
REMOVE_GRACE = 25  # podman inspect must fail this soon after running=false
REAP_SLACK = 15  # extra seconds beyond timeout + tick on reap deadlines
ALIVE_FACTOR = 2.2  # alive-expect window = factor x effective timeout
ALIVE_CAP = 110  # ... capped, so long-timeout ws can't stretch a wave
IDLE_SECS_TOL = 6  # +/- beyond tick for idle_seconds vs wall clock

# Global idle-timeout draws (seconds): small so runs stay bounded, including
# boundary values around the check interval (max(10, min(60, t//3))).
GLOBAL_TIMEOUTS = [5, 6, 8, 10, 12, 15, 18]
BOGUS_GLOBAL_P = 0.08  # rare: invalid env value -> must fall back to 3600

# Per-workspace override draws (seconds); 0 = never idle out.
SHORT_OVERRIDES = [2, 3, 4, 5, 6]
LONG_OVERRIDES = [25, 30, 35, 40]

PATTERNS = [
    "idle",
    "quiet_ws",
    "term_under",
    "term_over",
    "egress",
    "mixed",
    "stops_mid",
]
WS_PATTERNS = ("quiet_ws", "term_under", "term_over", "mixed", "stops_mid")


# ---------------------------------------------------------------------------
# Hermetic env (mirrors klangkd-tests/e2e-tests/_e2e_env.py)
# ---------------------------------------------------------------------------

_STRIP_PREFIXES = ("KLANGK", "_KLANGK", "KLANGKC", "LOGFIRE")
_INFRA_VARS = ("KLANGKD_IMAGE_NAME", "KLANGKD_VERSION_FILE")


def clean_env(**overrides: str) -> dict[str, str]:
    """Ambient env minus every config-affecting var, plus overrides."""
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith(_STRIP_PREFIXES)
    }
    for name in _INFRA_VARS:
        val = os.environ.get(name)
        if val is not None:
            env[name] = val
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------


class Server:
    """A klangkd subprocess fronted by its Caddy proxy (TCP), log to a file.

    TCP via the proxy (not UDS-direct) is required: the network sidecar's
    consent/activity WebSocket reaches klangkd at
    ``host.containers.internal:<KLANGKD_EGRESS_PORT>``, and that listener
    exists only when the proxy runs (klangkd itself binds the UDS; the
    egress smoketest uses the same mode for the same reason). Without it,
    egress-activity bumps silently never arrive and #2479's bridge would
    look broken when it is the harness that is misconfigured.
    """

    def __init__(self, *, data_dir: str, state_dir: str, log_path: str):
        self.data_dir = data_dir
        self.state_dir = state_dir
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.ws_base = ""
        self._log_scan_offset = 0

    def start(self, **env_overrides: str) -> None:
        # Distinct free ports for the browser (API/WS) + egress listeners;
        # two draws can collide on a busy host, so redraw until distinct.
        port = free_port()
        egress_port = free_port()
        while egress_port == port:
            egress_port = free_port()
        self.url = f"http://localhost:{port}"
        self.ws_base = f"ws://localhost:{port}"
        env = clean_env(
            KLANGKD_DATA_DIR=self.data_dir,
            KLANGKD_STATE_DIR=self.state_dir,
            KLANGKD_PORT=str(port),
            KLANGKD_EGRESS_PORT=str(egress_port),
            KLANGKD_AUTH_MODES="password",
            KLANGKD_JWT_SECRET="idle-fuzz-secret",
            KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
            KLANGKD_DEFAULT_USER="idle@example.com",
            KLANGKD_DEFAULT_PASSWORD="idlepass",
            KLANGKD_TEST_MODE="1",  # /test/set-idle-timeout (runtime changes)
            # Prompt sidecar activity bumps at fuzz timescale (#2485): the
            # default 60s gate would starve egress keep-alive between checks
            # when timeouts are single-digit seconds (forwarded by
            # ContainerManager.start_network_sidecar).
            KLANGKNETWORK_EGRESS_ACTIVITY_GATE="1",
            LOGFIRE_TOKEN="",
            **env_overrides,
        )
        env.pop("KLANGKD_FRONTEND_DIR", None)
        log_file = open(self.log_path, "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            ["python3", "-m", "klangk.main", "--config=none"],
            cwd=os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "src",
                "klangk",
            ),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        self.proc._log_file = log_file  # type: ignore[attr-defined]
        self._wait_ready()

    def _health_ok(self, client: httpx.Client) -> bool:
        """One /health probe; False on a transport error."""
        try:
            return client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def _wait_ready(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.url, timeout=2) as client:
            while time.monotonic() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    raise RuntimeError(
                        f"klangkd exited early:\n{self._read_log()[-4000:]}"
                    )
                if self._health_ok(client):
                    return
                time.sleep(0.5)
        raise RuntimeError(
            f"klangkd not healthy within {timeout}s:\n{self._read_log()[-4000:]}"
        )

    def _read_log(self) -> str:
        try:
            with open(self.log_path) as fh:
                return fh.read()
        except OSError:
            return ""

    def scan_log_anomalies(self) -> list[str]:
        """New log lines that look like unhandled exceptions.

        INFO/WARNING lines are excluded -- an invalid
        KLANGKD_IDLE_TIMEOUT_SECONDS draw legitimately warns at startup.
        """
        text = self._read_log()
        new = text[self._log_scan_offset :]
        self._log_scan_offset = len(text)
        return [line.strip() for line in new.splitlines() if log_line_is_anomaly(line)]

    def stop(self) -> None:
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            log_file = getattr(proc, "_log_file", None)
            if log_file is not None:
                with contextlib.suppress(Exception):
                    log_file.close()
        self._cleanup_containers()

    def _instance_id(self) -> str:
        """This instance's id from the data dir ("" when absent/unreadable)."""
        try:
            with open(os.path.join(self.data_dir, "instance-id")) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _rm_role_containers(self, instance_id: str, role: str) -> None:
        """Force-remove every podman container labelled with this
        instance+role."""
        result = subprocess.run(
            [
                PODMAN,
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=klangk.instance={instance_id}",
                "--filter",
                f"label=klangk.role={role}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ids = result.stdout.split()
        if ids:
            subprocess.run(
                [PODMAN, "rm", "-f", *ids],
                capture_output=True,
                timeout=60,
            )

    def _cleanup_containers(self) -> None:
        """Remove any podman containers labelled with this instance's id."""
        instance_id = self._instance_id()
        if not instance_id:
            return
        try:
            # Workspaces first, then sidecars: a workspace shares its
            # sidecar's netns, so podman refuses to remove a sidecar with
            # a live dependent (#2476).
            for role in ("workspace", "network-sidecar"):
                self._rm_role_containers(instance_id, role)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def log_line_is_anomaly(line: str) -> bool:
    """Does a log line look like an unhandled exception? INFO/WARNING lines
    are excluded -- an invalid KLANGKD_IDLE_TIMEOUT_SECONDS draw legitimately
    warns at startup."""
    low = line.lower()
    has_kw = any(kw in low for kw in ("traceback", "unhandled", "exception", "fatal"))
    return has_kw and "INFO" not in line and "WARNING" not in line


class Anomalies:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, kind: str, detail: str, **extra) -> None:
        self.items.append({"kind": kind, "detail": detail, **extra})
        logger.warning("ANOMALY [%s] %s", kind, detail)

    def __bool__(self) -> bool:
        return bool(self.items)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class Api:
    """Async API client over the server's proxy port; records 5xx."""

    def __init__(self, server: Server, anomalies: Anomalies):
        self.anomalies = anomalies
        self.token = ""
        self.client = httpx.AsyncClient(base_url=server.url, timeout=15)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = await self.client.request(method, path, json=json_body, headers=headers)
        if resp.status_code >= 500:
            self.anomalies.add(
                "server-5xx",
                f"{method} {path} -> {resp.status_code}: {resp.text[:300]}",
            )
        return resp

    async def login(self) -> None:
        resp = await self.request(
            "POST",
            "/api/v1/auth/login",
            json_body={
                "identifier": "idle@example.com",
                "password": "idlepass",
            },
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]

    async def create_workspace(self, name: str) -> str:
        resp = await self.request(
            "POST", "/api/v1/workspaces", json_body={"name": name}
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def patch_settings(self, ws_id: str, settings: dict) -> httpx.Response:
        return await self.request(
            "PATCH",
            f"/api/v1/workspaces/{ws_id}/settings",
            json_body=settings,
        )

    async def start(self, ws_id: str) -> httpx.Response:
        return await self.request("POST", f"/api/v1/workspaces/{ws_id}/start")

    async def status(self, ws_id: str) -> dict | None:
        resp = await self.request("GET", f"/api/v1/workspaces/{ws_id}/status")
        if resp.status_code != 200:
            return None
        return resp.json()

    async def workspace_row_exists(self, ws_id: str) -> bool:
        """True while the workspace row survives (GET /workspaces/{id} does
        not exist as a route; check the list instead)."""
        resp = await self.request("GET", "/api/v1/workspaces")
        if resp.status_code != 200:
            return False
        try:
            rows = resp.json()
        except ValueError:
            return False
        if isinstance(rows, dict):  # a paged shape
            rows = rows.get("workspaces", [])
        return any(r.get("id") == ws_id for r in rows)

    async def delete(self, ws_id: str) -> None:
        try:
            await asyncio.wait_for(
                self.request("DELETE", f"/api/v1/workspaces/{ws_id}"),
                timeout=DELETE_TIMEOUT,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            logger.warning("DELETE %s failed: %r", ws_id, exc)

    async def test_set_idle_timeout(self, ws_id: str, seconds: int) -> httpx.Response:
        return await self.request(
            "POST",
            "/api/v1/test/set-idle-timeout",
            json_body={"seconds": seconds, "workspace_id": ws_id},
        )


# ---------------------------------------------------------------------------
# Podman helpers
# ---------------------------------------------------------------------------


def is_container_gone_error(exc: BaseException) -> bool:
    """A podman exec failed because the target container vanished (rc 125)."""
    return isinstance(exc, subprocess.CalledProcessError) and exc.returncode == 125


async def container_exists(cid: str) -> bool:
    """True while podman still knows the container (ps -a / inspect)."""
    proc = await asyncio.create_subprocess_exec(
        PODMAN,
        "inspect",
        cid,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


def podman_exec(cid: str, script: str) -> None:
    """Run a shell snippet inside the container (raises on failure)."""
    subprocess.run(
        [PODMAN, "exec", cid, "bash", "-c", script],
        check=True,
        capture_output=True,
        timeout=20,
    )


# ---------------------------------------------------------------------------
# Terminal-over-WS helper
# ---------------------------------------------------------------------------


class WsTerminal:
    """A browser-ish WS session: workspace_connect + terminal, frame drain."""

    def __init__(self, server: Server, token: str, ws_id: str):
        self.server = server
        self.token = token
        self.ws_id = ws_id
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.frame_types: list[str] = []

    async def connect(self, timeout: float = BRINGUP_TIMEOUT) -> None:
        # #3201: the JWT rides the handshake's subprotocol header, not
        # the URL query string.
        self.ws = await websockets.connect(
            f"{self.server.ws_base}/ws",
            max_size=2**20,
            subprotocols=["bearer", self.token],
        )
        await self.ws.send(
            json.dumps({"cmd": "workspace_connect", "workspaceId": self.ws_id})
        )
        deadline = time.monotonic() + timeout
        ready = False
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            self._note(raw)
            if self._frame_type(raw) == "container_ready":
                ready = True
                break
        if not ready:
            await self.close()
            raise RuntimeError("container_ready not received in time")
        await self.ws.send(json.dumps({"cmd": "ui_ready"}))
        self.reader = asyncio.create_task(self._drain())

    async def start_terminal(self) -> None:
        await self.ws.send(
            json.dumps(
                {
                    "cmd": "terminal_start",
                    "cols": 80,
                    "rows": 24,
                    "browser_id": f"idlefuzz-{uuid.uuid4().hex[:8]}",
                }
            )
        )

    async def send_input(self, data: str) -> None:
        await self.ws.send(json.dumps({"cmd": "terminal_input", "data": data}))

    async def _drain(self) -> None:
        try:
            async for raw in self.ws:
                self._note(raw)
        except websockets.ConnectionClosed:
            pass

    def _note(self, raw) -> None:
        self.frame_types.append(self._frame_type(raw))

    @staticmethod
    def _frame_type(raw) -> str:
        try:
            return str(json.loads(raw).get("type"))
        except Exception:
            return "?"

    async def close(self) -> None:
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None


# ---------------------------------------------------------------------------
# Scenario model
# ---------------------------------------------------------------------------


@dataclass
class WsSpec:
    """One workspace's drawn configuration for a wave."""

    name: str
    pattern: str
    override: int | None = None  # per-workspace idle_timeout (None = global)
    invalid_patch: bool = False  # first PATCH invalid values (expect 4xx)
    # Runtime change via /test/set-idle-timeout at ~0.5x the orig timeout:
    # ("shorten", 3) | ("lengthen", 30) | ("zero", 0) | None
    runtime_change: tuple[str, int] | None = None
    # Filled by the wave planner:
    eff_timeout: int = 0  # effective timeout at start
    expect: str = "reap"  # "reap" | "alive"
    loose_alive: bool = False  # alive with no observable deadline (3600s)
    timeline: list = field(default_factory=list)
    ws_id: str = ""

    def note(self, event: str) -> None:
        self.timeline.append((round(time.monotonic(), 1), event))

    def scenario_json(self) -> dict:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "override": self.override,
            "runtime_change": self.runtime_change,
            "eff_timeout": self.eff_timeout,
            "expect": self.expect,
        }


def tick_with_overrides(values: list[int]) -> int:
    """The cleanup loop's tick while per-workspace overrides are live:
    ``max(2, min(values)//2)`` (container.py)."""
    return max(2, min(values) // 2)


def global_tick(global_timeout: int) -> int:
    """The cleanup loop's tick with no per-workspace overrides:
    ``max(10, min(60, t//3))`` (container.py)."""
    return max(10, min(60, global_timeout // 3))


def live_override_values(specs: list[WsSpec]) -> list[int]:
    """Every override value that can ever be live in the wave: the initial
    per-workspace overrides plus the runtime-change values."""
    values = [s.override for s in specs if s.override is not None]
    values += [s.runtime_change[1] for s in specs if s.runtime_change is not None]
    return values


def wave_tick_bound(specs: list[WsSpec], global_timeout: int) -> int:
    """A conservative upper bound on the cleanup loop's tick for the wave.

    The loop recomputes its tick each pass from the live per-workspace
    overrides; each live value is either an initial override or a runtime
    change value, and ``tick(S) = max(2, min(S)//2) <= max(2, v//2)`` for
    any ``v in S``. So the max of ``v//2`` over every value that can ever
    be live (plus the no-override global tick) bounds every phase.
    """
    bounds = [global_tick(global_timeout), 2]
    bounds += [v // 2 for v in live_override_values(specs)]
    return max(bounds)


def draw_override(rng: random.Random, bogus_global: bool) -> int | None:
    """Draw the per-workspace override. With a bogus global (falls back to
    3600s) a "none" override can never reap inside a run, so force an
    override there."""
    if bogus_global:
        return rng.choice([*SHORT_OVERRIDES, *LONG_OVERRIDES, 0])
    draw = rng.choices([None, "short", "long", 0], weights=[3, 3, 2, 1])[0]
    if draw == "short":
        return rng.choice(SHORT_OVERRIDES)
    if draw == "long":
        return rng.choice(LONG_OVERRIDES)
    return draw  # None or 0


def runtime_change_tuple(kind: str | None) -> tuple[str, int] | None:
    """The ``(kind, value)`` change for a drawn kind (None -> no change)."""
    if kind == "shorten":
        return ("shorten", 3)
    if kind == "lengthen":
        return ("lengthen", 30)
    if kind == "zero":
        return ("zero", 0)
    return None


def draw_runtime_change(rng: random.Random, pattern: str):
    """Draw the mid-flight change, only where the outcome stays
    deterministic: no-activity patterns may shorten (reap sooner must be
    observed); activity patterns may only lengthen/zero (activity keeps
    flowing, so a shorten would race the driver's own cadence)."""
    if pattern in ("idle", "quiet_ws"):
        kind = rng.choices([None, "shorten", "lengthen", "zero"], weights=[6, 2, 1, 1])[
            0
        ]
    elif pattern in ("term_under", "egress"):
        kind = rng.choices([None, "lengthen", "zero"], weights=[7, 1, 1])[0]
    else:
        kind = None
    return runtime_change_tuple(kind)


def apply_effective_timeout(
    spec: WsSpec, pattern: str, override: int | None, global_timeout: int
) -> int:
    """Set spec.eff_timeout (and floor tiny activity timeouts)."""
    eff = spec.override if spec.override is not None else global_timeout
    if (
        pattern in ("term_under", "egress", "mixed")
        and override is not None
        and 0 < eff < 6
    ):
        # A tiny timeout on an activity pattern gets reaped during the
        # harness's own bringup (terminal start, first status polls)
        # before the pattern can drive keep-alive -- floor it so the
        # "active workspace stays alive" invariant is testable.
        eff = 6
        spec.override = 6
    spec.eff_timeout = eff
    return eff


def spec_expects_alive(spec: WsSpec, pattern: str, eff: int) -> bool:
    """Does the drawn spec expect to stay alive (vs be reaped)? A zero
    override / a >=60s effective timeout never reaps in-window; an activity
    pattern keeps it alive; a lengthen/zero runtime change must not be
    reaped in-window."""
    if spec.override == 0 or eff >= 60:
        return True
    if pattern in ("term_under", "egress", "mixed"):
        return True
    return spec.runtime_change is not None and spec.runtime_change[0] in (
        "lengthen",
        "zero",
    )


def expected_outcome(spec: WsSpec, pattern: str, eff: int) -> str:
    """The observable expectation for the drawn spec."""
    if spec.override == 0 or eff >= 60:
        spec.loose_alive = eff >= 60
    if spec_expects_alive(spec, pattern, eff):
        return "alive"  # must not be reaped in-window
    return "reap"


def draw_wave(
    rng: random.Random,
    *,
    global_timeout: int,
    bogus_global: bool,
    max_concurrent: int,
) -> list[WsSpec]:
    """Draw one wave: 1..max_concurrent workspace specs."""
    n = min(rng.choices([1, 2, 3], weights=[4, 4, 2])[0], max_concurrent)
    specs = []
    for _ in range(n):
        pattern = rng.choice(PATTERNS)
        override = draw_override(rng, bogus_global)
        spec = WsSpec(
            name=f"idlefuzz-{uuid.uuid4().hex[:6]}",
            pattern=pattern,
            override=override,
            invalid_patch=rng.random() < 0.25,
        )
        spec.runtime_change = draw_runtime_change(rng, pattern)
        eff = apply_effective_timeout(spec, pattern, override, global_timeout)
        spec.expect = expected_outcome(spec, pattern, eff)
        specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


class Ctx:
    def __init__(self, server: Server, api: Api, anomalies: Anomalies):
        self.server = server
        self.api = api
        self.anomalies = anomalies


async def wait_running(
    ctx: Ctx, spec: WsSpec, timeout: float = BRINGUP_TIMEOUT
) -> dict:
    """Poll status until running; return the first running payload."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await ctx.api.status(spec.ws_id)
        if last is not None and last.get("running"):
            return last
        await asyncio.sleep(2)
    raise RuntimeError(f"container never became running; last={last!r}")


def check_idle_reported(
    ctx: Ctx, spec: WsSpec, reported, wall: float, tol: float, phase: str
) -> None:
    """Assert idle_seconds is numeric and tracks wall-clock idle."""
    if not isinstance(reported, (int, float)):
        ctx.anomalies.add(
            "idle-secs",
            f"{spec.name}: idle_seconds not numeric: {reported!r}",
            scenario=spec.scenario_json(),
        )
        return
    if abs(reported - wall) > tol:
        ctx.anomalies.add(
            "idle-secs",
            f"{spec.name} ({phase}): idle_seconds={reported} but wall "
            f"idle={wall:.1f}s (tolerance +/-{tol}s)",
            scenario=spec.scenario_json(),
        )


async def check_idle_seconds(
    ctx: Ctx, spec: WsSpec, last_activity: float, tick: int, phase: str
) -> None:
    """Sample idle_seconds once and compare with wall-clock idle."""
    st = await ctx.api.status(spec.ws_id)
    if st is None or not st.get("running"):
        return
    wall = time.monotonic() - last_activity
    if wall < tick + 2:
        return  # too close to the last bump to assert tightly
    check_idle_reported(
        ctx, spec, st.get("idle_seconds"), wall, tick + IDLE_SECS_TOL, phase
    )


async def probe_invalid_patches(ctx: Ctx, spec: WsSpec) -> None:
    """PATCH values the settings coercion must reject (workspace_settings.py
    _coerce_nonnegative_int): negative, non-numeric string, empty string,
    non-integer float. Each must 400, never 5xx."""
    for bad in (-5, "notanumber", "", 1.5):
        resp = await ctx.api.patch_settings(spec.ws_id, {"idle_timeout": bad})
        if not 400 <= resp.status_code < 500:
            ctx.anomalies.add(
                "invalid-patch",
                f"{spec.name}: PATCH idle_timeout={bad!r} -> "
                f"{resp.status_code} (expected 4xx)",
                scenario=spec.scenario_json(),
            )


async def apply_idle_override(ctx: Ctx, spec: WsSpec) -> None:
    """PATCH the per-workspace idle_timeout override."""
    resp = await ctx.api.patch_settings(spec.ws_id, {"idle_timeout": spec.override})
    if resp.status_code != 200:
        ctx.anomalies.add(
            "patch",
            f"{spec.name}: PATCH override {spec.override} -> {resp.status_code}",
            scenario=spec.scenario_json(),
        )


async def configure_workspace(ctx: Ctx, spec: WsSpec) -> None:
    """Apply the scenario's settings patches (invalid-probe + override)."""
    if spec.invalid_patch:
        await probe_invalid_patches(ctx, spec)
        spec.note("invalid-patches-sent")
    if spec.override is not None:
        await apply_idle_override(ctx, spec)
        spec.note(f"override={spec.override}")


async def start_ws_terminal(ctx: Ctx, spec: WsSpec) -> WsTerminal:
    """Connect a WS session + open a terminal for a WS-driven pattern
    (quiet_ws stops at the connect)."""
    term = WsTerminal(ctx.server, ctx.api.token, spec.ws_id)
    spec.note("ws-connect")
    await term.connect()
    if spec.pattern != "quiet_ws":
        await term.start_terminal()
        await asyncio.sleep(1)
        await term.send_input(" ")
    return term


async def api_start(ctx: Ctx, spec: WsSpec) -> None:
    """Start the workspace via the plain API (idle/egress patterns)."""
    spec.note("api-start")
    resp = await ctx.api.start(spec.ws_id)
    if resp.status_code != 200:
        ctx.anomalies.add(
            "start",
            f"{spec.name}: start -> {resp.status_code}",
        )


def check_status_timeout(ctx: Ctx, spec: WsSpec, st: dict) -> None:
    """Sanity: the status endpoint reports the effective timeout."""
    if st.get("idle_timeout") != spec.eff_timeout:
        ctx.anomalies.add(
            "status-timeout",
            f"{spec.name}: status idle_timeout="
            f"{st.get('idle_timeout')} != effective "
            f"{spec.eff_timeout}",
            scenario=spec.scenario_json(),
        )


async def start_scenario_workspace(
    ctx: Ctx, spec: WsSpec
) -> tuple[WsTerminal | None, dict]:
    """Start the workspace per its pattern; returns (terminal-or-None,
    running-status)."""
    term: WsTerminal | None = None
    if spec.pattern in WS_PATTERNS:
        term = await start_ws_terminal(ctx, spec)
    else:
        await api_start(ctx, spec)
    st = await wait_running(ctx, spec)
    cid = st.get("container_id") or ""
    spec.note(f"running cid={cid[:12]}")
    check_status_timeout(ctx, spec, st)
    return term, st


async def cancel_scenario_tasks(
    pattern_task: asyncio.Task, change_task: asyncio.Task | None
) -> None:
    """Cancel (and await) the pattern and runtime-change tasks."""
    pattern_task.cancel()
    if change_task is not None:
        change_task.cancel()
    for t in (pattern_task, change_task):
        if t is not None:
            with contextlib.suppress(BaseException):
                await t


async def apply_runtime_change(ctx: Ctx, api, spec: WsSpec, state: dict) -> None:
    """Apply the mid-flight idle-timeout change and update the expected
    outcome (shorten -> reap restarts the deadline; lengthen/zero -> the
    workspace must stay alive)."""
    kind, value = spec.runtime_change
    await asyncio.sleep(max(1.0, spec.eff_timeout * 0.5))
    resp = await api.test_set_idle_timeout(spec.ws_id, value)
    if resp.status_code != 200:
        ctx.anomalies.add(
            "runtime-change",
            f"{spec.name}: /test/set-idle-timeout {value} -> {resp.status_code}",
        )
        return
    spec.note(f"runtime-change->{kind}:{value}")
    if kind == "shorten":
        # No-activity pattern: the reap deadline restarts from
        # the change (the wake event fires immediately).
        state["eff"] = value
        state["expect"] = "reap"
        state["last_activity"] = time.monotonic()
    elif kind == "lengthen":
        state["eff"] = spec.eff_timeout + value
    else:  # zero: never idle out
        state["eff"] = 0
        state["expect"] = "alive"


async def send_keepalive(
    ctx: Ctx, spec: WsSpec, state: dict, term, cid: str, seq: int
) -> None:
    """One iteration's keep-alive work (terminal input / egress DNS)."""
    if spec.pattern in ("term_under", "stops_mid", "mixed"):
        if term is not None:
            await term.send_input(" ")
        state["last_activity"] = time.monotonic()
    if spec.pattern in ("egress", "mixed"):
        # Blocking podman call off the loop: with up to
        # --max-concurrent workspaces a 20s exec timeout
        # must not stall the other scenarios.
        await asyncio.to_thread(
            podman_exec,
            cid,
            f"getent hosts f{seq}-{spec.name}.idlefuzz.invalid || true",
        )
        state["last_activity"] = time.monotonic()


def pattern_gaps(spec: WsSpec, tick: int) -> tuple[int, int, int]:
    """``(gap_under, gap_over, cadence)`` for the spec's effective timeout."""
    eff0 = spec.eff_timeout
    gap_under = max(1, eff0 - 2) if eff0 > 3 else 1
    gap_over = eff0 + tick + 6
    cadence = max(1, min(2, eff0 // 3)) if eff0 > 0 else 2
    return gap_under, gap_over, cadence


async def sleep_term_over(term, state: dict, gap_over: int) -> None:
    """term_over's burst: lands beyond timeout+tick, so the reaper must
    reap between bursts (timer reset)."""
    await asyncio.sleep(gap_over)
    if term is not None:
        await term.send_input(" ")
    state["last_activity"] = time.monotonic()


def stops_mid_gone_silent(spec: WsSpec, seq: int, gap_under: int) -> bool:
    """stops_mid: silent after enough bursts to have been keep-alive."""
    bursts = max(2, int(spec.eff_timeout * 1.5 / gap_under))
    return seq >= bursts


async def sleep_stops_mid(spec: WsSpec, seq: int, gap_under: int) -> bool:
    """stops_mid's tick: silent (False) after enough bursts to have been
    keep-alive."""
    if stops_mid_gone_silent(spec, seq, gap_under):
        spec.note("gone-silent")
        return False  # silence -> reap expected
    await asyncio.sleep(gap_under)
    return True


async def pattern_sleep(
    spec: WsSpec,
    state: dict,
    term,
    seq: int,
    gap_under: int,
    gap_over: int,
    cadence: int,
) -> bool:
    """Sleep per pattern; False when the driver goes silent (stops_mid)."""
    if spec.pattern in ("idle", "quiet_ws"):
        await asyncio.sleep(3600)  # silence is the point
    elif spec.pattern == "term_under":
        await asyncio.sleep(gap_under)
    elif spec.pattern == "term_over":
        await sleep_term_over(term, state, gap_over)
    elif spec.pattern == "stops_mid":
        return await sleep_stops_mid(spec, seq, gap_under)
    else:  # egress / mixed
        await asyncio.sleep(cadence)
    return True


async def run_activity_pattern(
    ctx: Ctx,
    spec: WsSpec,
    state: dict,
    term: WsTerminal | None,
    cid: str,
    gone: asyncio.Event,
    stop_pattern: asyncio.Event,
    tick: int,
) -> None:
    """Drive the scenario's keep-alive / silence pattern until stopped,
    the container vanishes, or a podman-exec error lands."""
    gap_under, gap_over, cadence = pattern_gaps(spec, tick)
    seq = 0
    while not stop_pattern.is_set():
        seq += 1
        try:
            await send_keepalive(ctx, spec, state, term, cid, seq)
        except subprocess.CalledProcessError as exc:
            if is_container_gone_error(exc):
                gone.set()
                return  # container vanished mid-pattern
            ctx.anomalies.add(
                "pattern-error",
                f"{spec.name}: podman exec rc={exc.returncode}",
                scenario=spec.scenario_json(),
            )
            return
        if not await pattern_sleep(
            spec, state, term, seq, gap_under, gap_over, cadence
        ):
            return  # gone-silent (stops_mid)


async def launch_scenario_tasks(
    ctx: Ctx, spec: WsSpec, state: dict, term, cid: str, tick: int
) -> tuple:
    """Create the runtime-change + activity-pattern tasks. Returns
    ``(pattern_task, change_task, gone, stop_pattern)``."""
    change_task: asyncio.Task | None = None
    if spec.runtime_change is not None:
        change_task = asyncio.create_task(
            apply_runtime_change(ctx, ctx.api, spec, state)
        )
    gone = asyncio.Event()
    stop_pattern = asyncio.Event()
    pattern_task = asyncio.create_task(
        run_activity_pattern(ctx, spec, state, term, cid, gone, stop_pattern, tick)
    )
    return pattern_task, change_task, gone, stop_pattern


async def observe_scenario(
    ctx: Ctx, spec: WsSpec, state: dict, tick: int, gone: asyncio.Event, cid: str
) -> None:
    """Supervise the observers: a runtime change can flip the expected
    outcome mid-flight (shorten -> reap, lengthen/zero -> alive), so the
    observer is re-picked whenever ``expect`` changes; each observer returns
    False on a flip (unfinished) and True when done."""
    while True:
        if state["expect"] == "reap":
            done = await _observe_reap(ctx, spec, state, tick, gone, cid)
        else:
            done = await _observe_alive(ctx, spec, state, tick, gone)
        if done:
            return


async def cleanup_scenario(api: Api, spec: WsSpec, term) -> None:
    """Close the terminal + delete the workspace (row + container)."""
    if term is not None:
        await term.close()
    if spec.ws_id:
        await api.delete(spec.ws_id)
        spec.note("deleted")


async def run_ws_scenario(ctx: Ctx, spec: WsSpec, tick: int) -> None:
    """One workspace lifecycle: create -> configure -> start -> activity
    pattern -> invariants -> restart check -> cleanup."""
    spec.note("create")
    spec.ws_id = await ctx.api.create_workspace(spec.name)
    term: WsTerminal | None = None
    try:
        # -- configure ------------------------------------------------------
        await configure_workspace(ctx, spec)

        # -- start ----------------------------------------------------------
        term, st = await start_scenario_workspace(ctx, spec)
        cid = st.get("container_id") or ""

        state = {
            "last_activity": time.monotonic(),  # bringup is itself activity
            "eff": spec.eff_timeout,
            "expect": spec.expect,
        }

        # -- optional runtime change, applied mid-flight ----------------------
        # -- activity pattern + observation -----------------------------------
        pattern_task, change_task, gone, stop_pattern = await launch_scenario_tasks(
            ctx, spec, state, term, cid, tick
        )
        try:
            await observe_scenario(ctx, spec, state, tick, gone, cid)
        finally:
            stop_pattern.set()
            await cancel_scenario_tasks(pattern_task, change_task)
    except Exception as exc:  # noqa: BLE001
        ctx.anomalies.add(
            "scenario-error",
            f"{spec.name}: {exc!r}",
            scenario=spec.scenario_json(),
            timeline=spec.timeline[-20:],
        )
    finally:
        await cleanup_scenario(ctx.api, spec, term)


async def confirm_container_removed(ctx: Ctx, spec: WsSpec, cid: str) -> None:
    """After the reap the container must be fully removed from podman (no
    stopped-but-not-removed leaks)."""
    if not cid:
        return
    removed_by = time.monotonic() + REMOVE_GRACE
    while time.monotonic() < removed_by:
        if not await container_exists(cid):
            return
        await asyncio.sleep(2)
    ctx.anomalies.add(
        "not-removed",
        f"{spec.name}: container {cid[:12]} still in podman "
        f"ps -a {REMOVE_GRACE}s after running=false "
        "(stopped-but-not-removed leak)",
        scenario=spec.scenario_json(),
    )


async def confirm_reap_cleanup(ctx: Ctx, spec: WsSpec, cid: str) -> None:
    """After the reap: the container is fully removed and the row survives."""
    await confirm_container_removed(ctx, spec, cid)
    if not await ctx.api.workspace_row_exists(spec.ws_id):
        ctx.anomalies.add(
            "row-gone",
            f"{spec.name}: workspace row missing after reap (must survive)",
            scenario=spec.scenario_json(),
        )


async def await_restart_running(api: Api, ws_id: str) -> bool:
    """True once the workspace is running again within RESTART_TIMEOUT."""
    deadline = time.monotonic() + RESTART_TIMEOUT
    while time.monotonic() < deadline:
        st = await api.status(ws_id)
        if st is not None and st.get("running"):
            return True
        await asyncio.sleep(2)
    return False


async def confirm_restart(ctx: Ctx, spec: WsSpec) -> None:
    """...and the workspace restarts cleanly after the reap."""
    resp = await ctx.api.start(spec.ws_id)
    if resp.status_code != 200:
        ctx.anomalies.add(
            "restart-failed",
            f"{spec.name}: POST /start after reap -> {resp.status_code}",
            scenario=spec.scenario_json(),
        )
        return
    if await await_restart_running(ctx.api, spec.ws_id):
        spec.note("restarted-ok")
    else:
        ctx.anomalies.add(
            "restart-failed",
            f"{spec.name}: not running within {RESTART_TIMEOUT}s of restart after reap",
            scenario=spec.scenario_json(),
        )


async def running_status(api: Api, ws_id: str) -> bool | None:
    """The /status running flag, or None when the status call itself failed
    (a non-200) -- callers treat that as "keep waiting", not as a verdict."""
    st = await api.status(ws_id)
    if st is None:
        return None
    return bool(st.get("running"))


async def reap_wait_tick(
    ctx: Ctx, spec: WsSpec, la: float, tick: int, checked: bool
) -> bool:
    """Still running inside the reap window: one idle_seconds check + a
    sleep. Returns the updated checked flag."""
    if not checked:
        await check_idle_seconds(ctx, spec, la, tick, "reap-wait")
        checked = True
    await asyncio.sleep(2)
    return checked


async def finish_reap(ctx: Ctx, spec: WsSpec, cid: str) -> None:
    """running=false (or gone): the reap happened -- confirm full removal
    plus a clean restart."""
    spec.note("reaped")
    await confirm_reap_cleanup(ctx, spec, cid)
    await confirm_restart(ctx, spec)


async def _observe_reap(
    ctx: Ctx,
    spec: WsSpec,
    state: dict,
    tick: int,
    gone: asyncio.Event,
    cid: str,
) -> bool:
    """Expect stopped+removed within timeout + tick + slack of the last
    activity; the row survives; a restart returns to running. Returns
    False (unfinished) when a runtime change flipped the expectation."""
    checked_idle = False
    while True:
        if state["expect"] != "reap":
            return False  # flipped (lengthen/zero) -> supervisor re-picks
        la, eff = state["last_activity"], state["eff"]
        if time.monotonic() > la + eff + tick + REAP_SLACK:
            ctx.anomalies.add(
                "not-reaped",
                f"{spec.name}: not reaped "
                f"{time.monotonic() - la:.0f}s after last activity "
                f"(timeout={eff}s, tick<={tick}s, slack={REAP_SLACK}s)",
                scenario=spec.scenario_json(),
                timeline=spec.timeline[-20:],
            )
            return True
        if await running_status(ctx.api, spec.ws_id):
            checked_idle = await reap_wait_tick(ctx, spec, la, tick, checked_idle)
            continue
        await finish_reap(ctx, spec, cid)
        return True


def alive_window(spec: WsSpec, eff0: int) -> float:
    """The observation window for an alive expectation: a nominal 30s when
    there is no meaningful deadline (loose/3600s fallback, or a zero
    timeout), else factor x effective timeout (capped)."""
    if spec.loose_alive or eff0 == 0:
        return 30
    return min(ALIVE_FACTOR * max(eff0, 1), ALIVE_CAP)


def report_vanished_while_active(ctx: Ctx, spec: WsSpec, eff: int) -> None:
    """Anomaly: the container vanished while the pattern was still driving
    activity."""
    ctx.anomalies.add(
        "reaped-while-active",
        f"{spec.name}: container vanished while the pattern was "
        f"still driving activity (timeout={eff}s, "
        f"pattern={spec.pattern})",
        scenario=spec.scenario_json(),
        timeline=spec.timeline[-20:],
    )


def report_stopped_while_active(ctx: Ctx, spec: WsSpec, la: float, eff: int) -> None:
    """Anomaly: running=false while active (a stopped container can't be
    un-stopped by later activity)."""
    ctx.anomalies.add(
        "reaped-while-active",
        f"{spec.name}: running=false while active (timeout={eff}s, "
        f"pattern={spec.pattern}, {time.monotonic() - la:.0f}s "
        f"after last activity)",
        scenario=spec.scenario_json(),
        timeline=spec.timeline[-20:],
    )


async def alive_violation(
    ctx: Ctx, spec: WsSpec, state: dict, gone: asyncio.Event
) -> bool:
    """True (reporting an anomaly) when the workspace was reaped while
    active: the container vanished (podman exec rc 125), or running=false
    (a stopped container can't be un-stopped by later activity)."""
    la, eff = state["last_activity"], state["eff"]
    if gone.is_set():
        report_vanished_while_active(ctx, spec, eff)
        return True
    if await running_status(ctx.api, spec.ws_id) is False:
        report_stopped_while_active(ctx, spec, la, eff)
        return True
    return False


async def _observe_alive(
    ctx: Ctx,
    spec: WsSpec,
    state: dict,
    tick: int,
    gone: asyncio.Event,
) -> bool:
    """Expect still running well past the effective timeout while active.

    The window anchors at observation START (not last activity -- the
    pattern keeps refreshing that, so a last-activity anchor would never
    expire for a continuously-active workspace). Returns False (unfinished)
    when a runtime change flipped the expectation to reap."""
    started = time.monotonic()
    window = alive_window(spec, state["eff"])
    while True:
        if state["expect"] != "alive":
            return False  # flipped (shorten) -> supervisor re-picks
        if time.monotonic() > started + window:
            spec.note("alive-ok")
            return True
        if await alive_violation(ctx, spec, state, gone):
            return True
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def lengthened_timeout(s: WsSpec) -> int:
    """The effective timeout counting a lengthen runtime change."""
    eff = s.eff_timeout
    if s.runtime_change and s.runtime_change[0] == "lengthen":
        eff += s.runtime_change[1]
    return eff


def spec_observe_cost(s: WsSpec, tick: int) -> float:
    """Rough upper bound on one spec's observation window: the reap window
    (timeout + tick + slack + removal grace) for a reap expectation, else
    the alive window (a nominal 30s when there is no meaningful deadline)."""
    if s.expect != "alive":
        return s.eff_timeout + tick + REAP_SLACK + REMOVE_GRACE
    if s.loose_alive or s.eff_timeout == 0:
        return 30
    eff = lengthened_timeout(s)
    return min(ALIVE_FACTOR * max(eff, 1), ALIVE_CAP)


def est_wave_cost(specs: list[WsSpec], tick: int) -> float:
    """Rough upper bound on one wave's wall time (for budget planning)."""
    bringup = 60
    obs = max(spec_observe_cost(s, tick) for s in specs) if specs else 0.0
    return bringup + obs + 90  # + restart/delete headroom


def draw_global_timeout(rng: random.Random) -> tuple[bool, str, int]:
    """Draw the deploy-wide idle timeout (sometimes bogus: unparseable
    values fall back to the 3600s default)."""
    bogus_global = rng.random() < BOGUS_GLOBAL_P
    global_raw = (
        rng.choice(["twelve", "10s", ""])
        if bogus_global
        else str(rng.choice(GLOBAL_TIMEOUTS))
    )
    global_timeout = 3600 if bogus_global else int(global_raw)
    return bogus_global, global_raw, global_timeout


def report_anomalies(anomalies: Anomalies) -> int:
    """Print the anomaly verdict; the exit code (0 clean / 1 anomalies)."""
    if not anomalies:
        print("\nANOMALIES: 0")
        return 0
    print(f"\nANOMALIES: {len(anomalies.items)}")
    for item in anomalies.items[:50]:
        print(f"  [{item['kind']}] {item['detail']}")
    if len(anomalies.items) > 50:
        print(f"  ... and {len(anomalies.items) - 50} more")
    print("\nRESULT: ANOMALIES FOUND")
    print("=" * 60)
    return 1


def over_budget(args, cost: float, deadline: float) -> bool:
    """Would the next wave exceed the duration budget? (--waves overrides
    the budget check.)"""
    if args.waves:
        return False
    return time.monotonic() + cost > deadline


def log_wave_plan(waves_run: int, specs: list[WsSpec], tick: int) -> None:
    """Print the wave's drawn plan."""
    print(f"-- wave {waves_run} (tick bound {tick}s)")
    for spec in specs:
        print(
            f"   {spec.name}: pattern={spec.pattern} "
            f"override={spec.override} eff={spec.eff_timeout}s "
            f"change={spec.runtime_change} -> {spec.expect}"
        )


async def run_wave(
    server: Server,
    anomalies: Anomalies,
    rng: random.Random,
    args,
    global_timeout: int,
    bogus_global: bool,
    waves_run: int,
    deadline: float,
    scenario_log: list[dict],
) -> Ctx | None:
    """Draw + run one wave; returns the fresh per-wave Ctx (fresh client +
    token per wave, mirroring fuzz-api.py), or None when the time budget
    says stop."""
    ctx = Ctx(server, Api(server, anomalies), anomalies)
    await ctx.api.login()
    specs = draw_wave(
        rng,
        global_timeout=global_timeout,
        bogus_global=bogus_global,
        max_concurrent=args.max_concurrent,
    )
    tick = wave_tick_bound(specs, global_timeout)
    cost = est_wave_cost(specs, tick)
    if over_budget(args, cost, deadline):
        print(
            f"stopping: next wave (~{cost:.0f}s) would exceed the "
            f"{args.duration}m budget"
        )
        return None
    scenario_log.append(
        {
            "wave": waves_run,
            "tick_bound": tick,
            "specs": [spec.scenario_json() for spec in specs],
        }
    )
    log_wave_plan(waves_run, specs, tick)
    await asyncio.gather(*(run_ws_scenario(ctx, spec, tick) for spec in specs))
    for hit in server.scan_log_anomalies():
        anomalies.add("server-log", hit, wave=waves_run)
    await ctx.api.aclose()
    return Ctx(server, Api(server, anomalies), anomalies)


def server_env_for(args, global_raw: str) -> dict[str, str]:
    """The klangkd env for the run: the drawn global idle timeout. Image
    default: the ambient KLANGKD_IMAGE_NAME (forwarded by clean_env for the
    devenv-built image) or the plain settings default; --image overrides."""
    env = {"KLANGKD_IDLE_TIMEOUT_SECONDS": global_raw}
    if args.image:
        env["KLANGKD_IMAGE_NAME"] = args.image
    return env


def waves_done(args, waves_run: int) -> bool:
    """--waves count reached?"""
    return bool(args.waves) and waves_run >= args.waves


async def run_waves(
    server: Server,
    anomalies: Anomalies,
    rng: random.Random,
    args,
    global_timeout: int,
    bogus_global: bool,
    deadline: float,
    scenario_log: list[dict],
) -> int:
    """Draw + run waves until the --waves count or the time budget says
    stop; returns the waves run."""
    waves_run = 0
    while True:
        if waves_done(args, waves_run):
            break
        ctx = await run_wave(
            server,
            anomalies,
            rng,
            args,
            global_timeout,
            bogus_global,
            waves_run,
            deadline,
            scenario_log,
        )
        if ctx is None:
            break
        waves_run += 1
    for hit in server.scan_log_anomalies():
        anomalies.add("server-log", hit, wave="final")
    return waves_run


def write_scenario_log(
    log_dir: str, seed: int, global_raw: str, scenario_log: list[dict]
) -> None:
    """Persist the drawn scenario plan for post-run diagnosis."""
    with open(os.path.join(log_dir, "scenarios.json"), "w") as fh:
        json.dump(
            {
                "seed": seed,
                "global_idle_env": global_raw,
                "waves": scenario_log,
            },
            fh,
            indent=2,
        )


def print_run_header(args, log_dir: str, global_raw: str, global_timeout: int) -> None:
    print("== klangk idle-timeout fuzz ==")
    print(f"seed={args.seed} duration={args.duration}m log_dir={log_dir}")
    print(
        f"global KLANGKD_IDLE_TIMEOUT_SECONDS={global_raw!r} "
        f"(effective {global_timeout}s)"
    )


def print_run_report(
    args, log_dir: str, global_raw: str, global_timeout: int, waves_run: int
) -> None:
    print()
    print("=" * 60)
    print("IDLE FUZZ REPORT")
    print("=" * 60)
    print(f"seed: {args.seed}")
    print(f"global idle timeout env: {global_raw!r} ({global_timeout}s)")
    print(f"waves run: {waves_run}")
    print(f"scenario log: {os.path.join(log_dir, 'scenarios.json')}")


async def run(args) -> int:
    rng = random.Random(args.seed)
    anomalies = Anomalies()

    log_dir = args.log_dir or tempfile.mkdtemp(prefix="klangk-idle-fuzz-")
    os.makedirs(log_dir, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="klangk-idle-fuzz-data-")
    state_dir = tempfile.mkdtemp(prefix="klangk-idle-fuzz-state-")
    server = Server(
        data_dir=data_dir,
        state_dir=state_dir,
        log_path=os.path.join(log_dir, "server.log"),
    )

    bogus_global, global_raw, global_timeout = draw_global_timeout(rng)
    server_env = server_env_for(args, global_raw)
    scenario_log: list[dict] = []
    deadline = time.monotonic() + args.duration * 60

    print_run_header(args, log_dir, global_raw, global_timeout)

    try:
        server.start(**server_env)
        print("klangkd is up")
        waves_run = await run_waves(
            server,
            anomalies,
            rng,
            args,
            global_timeout,
            bogus_global,
            deadline,
            scenario_log,
        )
    finally:
        write_scenario_log(log_dir, args.seed, global_raw, scenario_log)
        server.stop()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)

    print_run_report(args, log_dir, global_raw, global_timeout, waves_run)
    print(f"server log:   {server.log_path}")
    return report_anomalies(anomalies)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idle-timeout fuzz harness for klangk (#2514)"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Fuzz for this many minutes (default: 10)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed (default: random)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        help="Max workspaces per wave (default: 3)",
    )
    parser.add_argument(
        "--waves",
        type=int,
        default=None,
        help="Run exactly N waves then stop (overrides --duration)",
    )
    parser.add_argument(
        "--image",
        default=None,
        help=(
            "Workspace container image (KLANGKD_IMAGE_NAME override; the "
            "harness runs klangkd --config=none, so the dev klangkd.yaml "
            "default does not apply)"
        ),
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for server.log + scenarios.json (default: tempdir)",
    )
    args = parser.parse_args()
    args.seed = draw_seed(args.seed)

    # Line-buffered stdout so a killed run (CI timeout) still shows its
    # progress; SIGTERM -> SystemExit so the finally-teardown (server stop,
    # container sweep, scenario log) runs instead of leaking the child.
    sys.stdout.reconfigure(line_buffering=True)

    def _on_sigterm(signum, frame):
        raise SystemExit(f"terminated by signal {signum}")

    signal.signal(signal.SIGTERM, _on_sigterm)

    configure_logging()
    # websockets handshake noise is idle-specific (the sidecar WS loop).
    logging.getLogger("websockets").setLevel(logging.WARNING)

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
