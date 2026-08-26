"""Shared ``klangkd`` launcher + UDS/TCP clients for E2E suites (#1525).

Launches the **real production entry point** — ``python3 -m klangk.main``
(``klangkd``) — instead of the test-only ``runtestserver.py``, and contacts
the backend the way production does, closing the last gap between the test
harness and the real server (#1454, #1426):

* **UDS-direct** (default, ``uds=True``): the proxy suppressed
  (``_KLANGKD_DISABLE_PROXY=1``), ``klangkd`` binds
  ``<state_dir>/klangk.sock``, and the suite's ``httpx`` / ``websockets``
  clients connect over that UDS via ``httpx`` UDS transports and
  ``websockets.unix_connect``. Used by the Python backend suites whose
  clients are in-process (``httpx`` + ``websockets``), so they exercise the
  same UDS + ``_UDS_MODE`` trust boundary production relies on.

* **TCP via the proxy** (``uds=False``): the proxy is enabled on a free
  ``KLANGKD_PORT`` (``_KLANGKD_DISABLE_PROXY`` cleared) and clients hit
  ``http://localhost:<port>`` — the proxy proxies to the UDS upstream. Used by
  suites whose clients have no UDS mode: the CLI E2E suite (drives the real
  ``klangk`` binary via ``--server <url>``) and the frontend Playwright suite
  (a real browser). This is also the production client path, so it is still
  faithful — the request traverses proxy → UDS → klangkd.

Every server's env is built via :func:`_e2e_env.clean_env` (hermetic; no
``KLANGKD_*`` leak from the ambient env, #1526). Each server gets a unique
``KLANGKD_STATE_DIR`` so the UDS path never collides.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import time
from subprocess import Popen
from typing import Any

import httpx
import websockets

from _e2e_env import clean_env, close_popen_pipes
from klangk.model import free_port

# The launcher is invoked as a module (``python3 -m klangk.main``) from the
# klangkd-tests dir — the same cwd the prior runtestserver launches used, so
# the subprocess resolves the installed ``klangk`` package and any relative
# test assets identically.
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")

# The repo's built Flutter web output. The e2e klangkd runs --config=none
# (env-vars-only), so it can't pick frontend_dir up from klangkd.yaml —
# provide it explicitly. Devenv no longer exports KLANGKD_FRONTEND_DIR
# (it lives in klangkd.yaml for the dev server, #1788).
FRONTEND_DIR = os.path.normpath(
    os.path.join(BACKEND_DIR, "..", "..", "frontend", "build", "web")
)

_mktemp_registry: list[str] = []


def rmtree_registered_temps() -> None:
    """Remove every dir :func:`tracked_mkdtemp` made (process exit, #2662)."""
    for path in _mktemp_registry:
        shutil.rmtree(path, ignore_errors=True)


def tracked_mkdtemp(prefix: str) -> str:
    """``tempfile.mkdtemp`` whose dir is removed at process exit (#2662).

    ``start_server``'s explicit ``stop_server`` teardown stays authoritative;
    this sweep is the safety net for dirs whose run never reaches it (a
    readiness timeout, a crashed test session) — they used to orphan in
    ``/tmp`` by the tens of thousands.
    """
    if not _mktemp_registry:
        atexit.register(rmtree_registered_temps)
    path = tempfile.mkdtemp(prefix=prefix)
    _mktemp_registry.append(path)
    return path


# A dummy host for UDS clients: the UDS transport ignores it for the
# connection, but httpx/websockets still need a syntactically valid URL
# (and the Host header value is irrelevant over a same-uid socket).
_UDS_HOST = "http://klangkd"
_UDS_WS_HOST = "ws://klangkd"

# How long to wait for the server to answer /health at startup. Container-less
# readiness is ~1-2s, but klangkd's startup includes the Podman pre-warm
# (a throwaway create+rm that initializes storage/userns), which can take
# 100s+ when the runner VM is contended — e.g. concurrent E2E suites each
# pre-warming the same podman session, or other load on the shared host.
# The server itself stays correct; it just crosses the line late, so give
# it room to finish (observed: pre-warm 105-110s + seed ~2s vs a 120s
# deadline that killed the server 2s before it came up).
_READINESS_TIMEOUT = 240

# File-streamed server logs from live ``start_server`` handles (#2623).
# Servers launched with ``log_path`` write their combined output to that
# file, which dies with the data dir at ``stop_server`` — so a failure whose
# cause lives server-side (e.g. an abrupt WS drop) left no evidence in the
# CI log. The e2e conftests read this registry (via ``_e2e_logs``) to attach
# each failing test's slice of the log to its pytest report. Pipe-captured
# servers (``log_path=None``) are not attachable while alive and stay out.
_active_log_paths: list[str] = []


def active_log_paths() -> tuple[str, ...]:
    """Paths of file-streamed klangkd logs owned by live server handles."""
    return tuple(_active_log_paths)


def _drain_stdout(proc: Popen, log_path: str | None = None) -> str:
    """Read the child's captured combined output (for failure diagnostics).

    When the server logs to a file (``log_path``), read that instead of the
    (None) pipe. The ``_wait_ready`` failure paths call this *after*
    terminating the child, so the file is fully flushed.
    """
    if log_path:
        try:
            with open(log_path) as fh:
                return fh.read()
        except OSError:
            return ""
    if proc.stdout is not None:
        try:
            return (proc.stdout.read() or b"").decode(errors="replace")
        except Exception:
            return ""
    return ""


def _terminate(proc: Popen) -> None:
    """Terminate a server so its captured stdout pipe EOFs and can be drained.

    Used by the readiness-timeout path in :func:`_wait_ready` before reading
    the child's captured output: a server that is still alive but not
    answering ``/health`` keeps its stdout pipe open, so a blocking
    ``proc.stdout.read()`` hangs until pytest-timeout fires (300s for the
    e2e suite) — masking the real 60s readiness failure *and* its
    diagnostics. Killing the child closes the write end of the pipe, so the
    drain returns the buffered startup output immediately.
    """
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if proc.poll() is None:  # SIGTERM ignored / not delivered
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _wait_ready(
    proc: Popen,
    *,
    uds_path: str | None,
    url: str | None,
    log_path: str | None = None,
) -> None:
    """Poll ``/health`` until the server is up, else kill + raise with logs."""
    if uds_path is not None:
        client = httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path), base_url=_UDS_HOST
        )
    else:
        assert url is not None
        client = httpx.Client(base_url=url)
    try:
        deadline = time.time() + _READINESS_TIMEOUT
        last_exc: Exception | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"klangkd exited early:\n{_drain_stdout(proc, log_path)}"
                )
            try:
                if client.get("/health", timeout=2).status_code == 200:
                    return
            except Exception as exc:  # not up yet
                last_exc = exc
            time.sleep(0.5)
        # Reaching here means the process is alive but never answered
        # /health — it has hung during startup (a crashed process would have
        # hit the ``proc.poll()`` branch above and already EOF'd its pipe).
        # Kill it before draining so the blocking read() returns the buffered
        # startup output instead of hanging until pytest-timeout fires.
        _terminate(proc)
        raise RuntimeError(
            f"klangkd did not become healthy within {_READINESS_TIMEOUT}s "
            f"(last error: {last_exc!r}):\n{_drain_stdout(proc, log_path)}"
        )
    finally:
        client.close()


def start_server(
    *,
    uds: bool = True,
    wait_ready: bool = True,
    data_dir: str | None = None,
    state_dir: str | None = None,
    config: str | None = None,
    log_path: str | None = None,
    **env_overrides: str,
) -> dict[str, Any]:
    """Launch real ``klangkd`` and block until it serves ``/health``.

    Parameters
    ----------
    uds:
        ``True`` (default) → UDS-direct: proxy suppressed, bind the socket at
        ``<state_dir>/klangk.sock``, return a UDS-configured ``client``. Use
        this for in-process Python clients.
        ``False`` → TCP via the proxy: the proxy on a free ``KLANGKD_PORT``, return a
    wait_ready:
        When ``False``, skip the ``/health`` readiness wait and return
        immediately after spawn — for tests whose server is EXPECTED to
        exit during startup (e.g. a past-due scheduled stop firing on
        boot); the default ``True`` treats that as a failure.
        ``url`` and a TCP ``client``. Use this for CLI / browser suites.
    data_dir, state_dir:
        Optional explicit dirs (created otherwise as tempdirs).
    config:
        Optional path to a YAML config file passed to ``klangkd --config``.
        ``None`` (default) → ``--config=none`` (env-vars-only). The SIGHUP
        config-reload E2E writes a YAML file and points klangkd at it.
    log_path:
        Optional path to redirect the server's combined stdout/stderr to a
        file instead of a captured pipe. The CLI / frontend E2E suites pass
        explicit paths; the default (``None``) derives one at
        ``<data_dir>/klangkd-test-output.log`` so every server's output survives on
        failure (``_e2e_logs`` attaches it to failing tests, #2623) instead
        of dying in a pipe that is only drained at process exit. Pass
        ``log_path=None`` is impossible after defaulting — pass an explicit
        path to override, or ``data_dir`` to control where the default lands.
        Pass ``log_path=""`` (empty string) to force the captured pipe.
    **env_overrides:
        Forwarded to :func:`_e2e_env.clean_env` as the test's explicit
        ``KLANGKD_*`` config (JWT secret, default user, auth mode, etc.).

    Returns a server handle dict with keys: ``proc``, ``data_dir``,
    ``state_dir``, ``uds_path`` (or ``None``), ``url`` (or ``None``), and
    ``client`` (a long-lived sync ``httpx.Client`` bound to the server —
    UDS transport when ``uds=True``, TCP ``base_url`` otherwise; helpers use
    ``server["client"]`` directly). Build additional/custom clients with
    :func:`httpx_client` / :func:`httpx_async_client`, and websockets with
    :func:`ws_connect`. Pass the handle to :func:`stop_server` for teardown.
    """
    if data_dir is None:
        data_dir = os.path.realpath(tracked_mkdtemp("klangk-e2e-"))
    if state_dir is None:
        state_dir = os.path.realpath(tracked_mkdtemp("klangk-e2e-state-"))
    # Default to a file-streamed log inside the data dir so the failure
    # hooks in ``_e2e_logs`` can attach what the server said (#2623); a
    # captured pipe is only drainable at process exit and vanishes with
    # the data dir. An explicit "" forces the old captured-pipe behavior
    # (the smoketest reads its log while the server runs). #364 still
    # applies: file streaming also avoids the 64 KB pipe-buffer deadlock.
    if log_path is None:
        log_path = os.path.join(data_dir, "klangkd-test-output.log")

    overrides = dict(env_overrides)
    overrides.setdefault("KLANGKD_DATA_DIR", data_dir)
    overrides.setdefault("KLANGKD_STATE_DIR", state_dir)
    overrides.setdefault("KLANGKD_FRONTEND_DIR", FRONTEND_DIR)
    overrides.setdefault("KLANGKD_PORT_RANGE_START", str(free_port()))

    uds_path: str | None
    url: str | None
    if uds:
        # Headless: no KLANGKD_PORT, proxy suppressed. klangkd binds the UDS.
        overrides.pop("KLANGKD_PORT", None)
        overrides.setdefault("_KLANGKD_DISABLE_PROXY", "1")
        uds_path = os.path.join(state_dir, "klangk.sock")
        url = None
    else:
        # The proxy fronts the UDS on a TCP port; clients hit the proxy. Both the
        # browser ingress (KLANGKD_PORT) and the container egress
        # (KLANGKD_EGRESS_PORT, default 8995) are allocated fresh so a test
        # never collides with a dev klangkd on the default egress port.
        # If the caller supplied KLANGKD_PORT, honor it (url derives from the
        # resolved port, not a separate free draw).
        overrides["_KLANGKD_DISABLE_PROXY"] = ""
        tcp_port = overrides.get("KLANGKD_PORT")
        if tcp_port is None:
            tcp_port = str(free_port())
            overrides["KLANGKD_PORT"] = tcp_port
        # Two independent free_port() draws can return the same port on a
        # busy runner (the OS reuses the just-released ephemeral port).
        # KLANGKD_EGRESS_PORT must differ from KLANGKD_PORT or the settings
        # validator rejects it and klangkd exits early — redraw until
        # distinct so the proxy's two listeners never collide.
        egress_port = str(free_port())
        while egress_port == tcp_port:
            egress_port = str(free_port())
        overrides.setdefault("KLANGKD_EGRESS_PORT", egress_port)
        uds_path = None
        url = f"http://localhost:{tcp_port}"

    env = clean_env(**overrides)
    cmd = ["python3", "-m", "klangk.main"]
    if config is not None:
        cmd += ["--config", config]
    else:
        cmd.append("--config=none")
    # When a log_path is given, stream the server's output to a file so a
    # long-lived run can't fill the 64 KB OS pipe buffer and deadlock (#364)
    # and the failure hooks can read it back (#2623). An empty string means
    # the caller explicitly wants the captured pipe (drained on failure).
    if log_path == "":
        log_file = None
    else:
        log_file = open(log_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        cmd,
        cwd=BACKEND_DIR,
        env=env,
        stdout=log_file if log_file is not None else subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Keep a reference so stop_server can close it; mirror the prior CLI
    # suite's ``proc._log_file`` convention.
    proc._log_file = log_file  # type: ignore[attr-defined]
    if wait_ready:
        _wait_ready(proc, uds_path=uds_path, url=url, log_path=log_path)

    client = httpx_client({"uds_path": uds_path, "url": url})
    if log_file is not None:
        _active_log_paths.append(log_path)
    return {
        "proc": proc,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "uds_path": uds_path,
        "url": url,
        "client": client,
        "log_path": log_path,
    }


def _cleanup_containers(data_dir: str) -> None:
    """Remove any podman containers labelled with this instance's id.

    The instance id is written to ``<data_dir>/instance-id`` at startup
    (#1553). A crashed/timed-out test can leave workspace containers behind;
    this best-effort sweep prevents them from accumulating across runs.

    Removal is two role-scoped passes — workspaces, then network sidecars —
    not one bulk ``podman rm -f``. A workspace joins its sidecar's netns via
    ``--network container:<sidecar>``, so podman refuses to remove a sidecar
    whose netns a live workspace still shares ("has dependent containers"),
    and ``-f`` does not override that. A single bulk removal puts sidecars
    and workspaces in an unspecified order, so a sidecar attempted before its
    workspace is skipped and left running (#2476). Removing the dependents
    first tears both down in one go, regardless of list order.
    """
    id_file = os.path.join(data_dir, "instance-id")
    instance_id = ""
    try:
        with open(id_file) as fh:
            instance_id = fh.read().strip()
    except OSError:
        pass
    if not instance_id:
        return
    try:
        for role in ("workspace", "network-sidecar"):
            result = subprocess.run(
                [
                    "podman",
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
            )
            ids = result.stdout.split()
            if ids:
                subprocess.run(
                    ["podman", "rm", "-f", *ids], capture_output=True
                )
    except FileNotFoundError:
        # podman not on PATH (e.g. a partial dev env) — nothing to clean.
        pass


def stop_server(server: dict[str, Any]) -> None:
    """Tear down a server started by :func:`start_server`.

    Kills the ``klangkd`` subprocess, removes its labelled containers, and
    deletes the data/state dirs. Safe to call from a ``finally``.
    """
    proc: Popen = server["proc"]
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    # Close the log file when one was opened (file-streamed stdout).
    log_file = getattr(proc, "_log_file", None)
    if log_file is not None:
        try:
            log_file.close()
        except Exception:
            pass
    close_popen_pipes(proc)
    try:
        server["client"].close()
    except Exception:
        pass
    # Unregister before the rmtree below deletes the log file (#2623).
    log_path = server.get("log_path")
    if log_path is not None and log_path in _active_log_paths:
        _active_log_paths.remove(log_path)
    data_dir = server["data_dir"]
    _cleanup_containers(data_dir)
    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(server["state_dir"], ignore_errors=True)


def httpx_client(server: dict[str, Any], **kwargs: Any) -> httpx.Client:
    """A sync ``httpx.Client`` bound to the server (UDS transport or TCP).

    Extra kwargs (e.g. ``timeout=``) are forwarded to ``httpx.Client``.
    """
    if server["uds_path"] is not None:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=server["uds_path"]),
            base_url=_UDS_HOST,
            **kwargs,
        )
    return httpx.Client(base_url=server["url"], **kwargs)


def httpx_async_client(
    server: dict[str, Any], **kwargs: Any
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` bound to the server (UDS transport or TCP)."""
    if server["uds_path"] is not None:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=server["uds_path"]),
            base_url=_UDS_HOST,
            **kwargs,
        )
    return httpx.AsyncClient(base_url=server["url"], **kwargs)


async def ws_connect(server: dict[str, Any], path: str, **kwargs: Any):
    """Open a websocket to ``path`` over the server's UDS or TCP transport.

    ``path`` is the request target including the query string, e.g.
    ``"/ws?token=..."``. Returns an open websocket connection (the caller
    closes it, typically via ``async with``).
    """
    if server["uds_path"] is not None:
        return await websockets.unix_connect(
            server["uds_path"], f"{_UDS_WS_HOST}{path}", **kwargs
        )
    ws_base = server["url"].replace("http://", "ws://")
    return await websockets.connect(f"{ws_base}{path}", **kwargs)
