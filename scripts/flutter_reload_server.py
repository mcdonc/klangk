#!/usr/bin/env python3
"""Extension-free auto-reload supervisor for the Flutter web dev server.

`flutter run -d web-server` compiles at **startup** (and on `r`/`R` hot
reload/restart, which require a debug-connected browser — i.e. the Chrome-only
Dart Debug Extension). A plain browser reload does **not** recompile. So to pick
up a source edit **without** the extension (e.g. in Firefox), this supervisor:

1. starts and owns the `flutter run -d web-server` process,
2. watches the frontend source,
3. on a save, **restarts** the dev server (warm `.dart_tool` cache → incremental
   recompile), and once it is serving again,
4. pushes a Server-Sent Events ``reload`` to connected browsers, which reload the
   tab via an injected ``EventSource`` client (works in Firefox/Safari/Chrome).

Trade-off vs the Dart Debug Extension: a dev-server restart (seconds) per save and
app state resets — but no extension and any browser. Dev-only; paired with
``KLANGK_WEB_DEV_RELOAD=1`` in ``scripts/nginx.sh`` (proxies ``/__livereload`` and
injects the client) and launched by ``scripts/flutterdevweb.sh``.

Env: KLANGK_WEB_DEV_RELOAD_PORT (SSE port, default 8994), KLANGK_WEB_DEV_PORT
(dev-server port, default 8996), KLANGK_WEB_FLUTTER (flutter binary), DEVENV_ROOT.
"""

from __future__ import annotations

import http.server
import os
import signal
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("DEVENV_ROOT") or ".").resolve() / "src" / "frontend"
WATCH_DIRS = [ROOT / "lib", ROOT / "web"]
SSE_PORT = int(os.environ.get("KLANGK_WEB_DEV_RELOAD_PORT", "8994"))
DEV_PORT = os.environ.get("KLANGK_WEB_DEV_PORT", "8996")
FLUTTER = os.environ.get("KLANGK_WEB_FLUTTER", "flutter")
POLL_SECONDS = 0.25
DEBOUNCE_SECONDS = 0.5
PING_SECONDS = 15.0
SERVE_MARKER = "is being served"

_generation = 0
_lock = threading.Lock()
_ready = threading.Event()
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()


def _flutter_cmd() -> list[str]:
    return [
        FLUTTER,
        "run",
        "-d",
        "web-server",
        "--web-hostname=127.0.0.1",
        f"--web-port={DEV_PORT}",
        "--no-web-resources-cdn",
    ]


def _start_flutter() -> None:
    global _proc
    _ready.clear()
    with _proc_lock:
        _proc = subprocess.Popen(
            _flutter_cmd(),
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        proc = _proc

    def _pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write("[flutter] " + line)
            sys.stdout.flush()
            if SERVE_MARKER in line:
                _ready.set()

    threading.Thread(target=_pump, daemon=True).start()


def _restart_flutter() -> None:
    with _proc_lock:
        proc = _proc
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _start_flutter()


def _snapshot() -> tuple[int, float]:
    count = 0
    latest = 0.0
    for d in WATCH_DIRS:
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    latest = max(latest, p.stat().st_mtime)
                    count += 1
                except OSError:
                    pass
    return count, latest


def _watch() -> None:
    global _generation
    last = _snapshot()
    pending_since: float | None = None
    while True:
        time.sleep(POLL_SECONDS)
        cur = _snapshot()
        if cur != last:
            last = cur
            pending_since = time.monotonic()
        elif pending_since is not None and (
            time.monotonic() - pending_since >= DEBOUNCE_SECONDS
        ):
            pending_since = None
            t0 = time.monotonic()
            print("livereload: change -> restarting dev server", flush=True)
            _restart_flutter()
            if _ready.wait(timeout=120):
                with _lock:
                    _generation += 1
                    gen = _generation
                dt = time.monotonic() - t0
                print(
                    f"livereload: dev server back in {dt:.1f}s -> reload (gen={gen})",
                    flush=True,
                )
            else:
                print("livereload: dev server did not come back in time", flush=True)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/__livereload":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        with _lock:
            seen = _generation
        last_ping = time.monotonic()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                time.sleep(0.5)
                with _lock:
                    cur = _generation
                if cur != seen:
                    seen = cur
                    self.wfile.write(b"data: reload\n\n")
                    self.wfile.flush()
                elif time.monotonic() - last_ping >= PING_SECONDS:
                    last_ping = time.monotonic()
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _shutdown(*_args) -> None:
    with _proc_lock:
        proc = _proc
    if proc and proc.poll() is None:
        proc.terminate()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    dirs = ", ".join(str(d) for d in WATCH_DIRS)
    print(
        f"livereload: supervising flutter dev server on :{DEV_PORT}, "
        f"SSE on 127.0.0.1:{SSE_PORT}/__livereload — watching {dirs}",
        flush=True,
    )
    _start_flutter()
    _ready.wait(timeout=120)
    threading.Thread(target=_watch, daemon=True).start()
    _Server(("127.0.0.1", SSE_PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()
