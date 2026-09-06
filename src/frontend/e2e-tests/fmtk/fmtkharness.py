"""Programmatic fmtk e2e harness (#3232).

The library behind the fmtk-driven frontend e2e suites: it boots the same
scratch stack ``fmtk-up`` builds (scratch klangkd + origin-splitting caddy
+ ``flutter run --debug -d chrome``), drives the running app through the
``fmtk`` CLI, and swaps server configuration live (SIGHUP, #1587) or via a
full restart — the capability every per-feature suite under this directory
builds on (parent #3231).

Reuse model, mirroring ``scripts/fmtk-up.sh``: the backend and proxy are
KEPT across runs (adopted when healthy and provably ours — same config
path), so re-runs pay only the flutter compile time. ``boot(fresh=True)``
wipes the scratch state first (fresh DB).

Stdlib + pyyaml (venv) only — it runs under the devenv venv python, like
``scripts/fmtk-seed.py``.

Env knobs (all optional, defaults match fmtk-up):

  FMTK_BACKEND_PORT (8998), FMTK_EGRESS_PORT (8996),
  FMTK_PROXY_PORT (8124), FMTK_FLUTTER_PORT (8125),
  FMTK_STATE (<repo>/.devenv/state/fmtk),
  FMTK_E2E_HEADLESS ("1" force headless Chrome, "0" force headed;
                     default auto: headless when no DISPLAY or CI=true)
"""

from __future__ import annotations

import asyncio
import json
import os
import quopri
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

FIXTURE_PASSWORD = "fmtk-Pass123!"
ADMIN_EMAIL = "fmtk-admin@example.com"

REPO_ROOT = Path(__file__).resolve().parents[4]
DEVENV_STATE = Path(os.environ.get("DEVENV_STATE") or REPO_ROOT / ".devenv/state")
STATE_DIR = Path(os.environ.get("FMTK_STATE") or DEVENV_STATE / "fmtk")
BACKEND_PORT = os.environ.get("FMTK_BACKEND_PORT", "8998")
EGRESS_PORT = os.environ.get("FMTK_EGRESS_PORT", "8996")
PROXY_PORT = os.environ.get("FMTK_PROXY_PORT", "8124")
FLUTTER_PORT = os.environ.get("FMTK_FLUTTER_PORT", "8125")
VENV_PYTHON = REPO_ROOT / ".devenv/state/venv/bin/python"

# Default scratch config — the exact file scripts/fmtk-up.sh writes. The
# harness owns this file once it boots the backend; swap_settings()
# rewrites it from the in-memory dict.
DEFAULT_CONFIG = {
    "port": BACKEND_PORT,
    "listen": "127.0.0.1",
    "egress_port": EGRESS_PORT,
    "idle_timeout_seconds": "300",
    "auth_modes": "password",
    "jwt_secret": "fmtk-scratch-secret",
    "default_user": "admin@example.com",
    "default_password": "admin123abc",
    # fmtk drives machine-speed /api bursts from one IP
    "api_rate_limit": "0",
    # ...and deliberate wrong-password scenarios (login failure modes,
    # step-up retries) every run: the default 5-failure lockout would
    # brick admin logins against a kept backend after a few runs
    "login_lockout_failures": "0",
    # Outbound email (verification / reset / invite tokens) goes to the
    # harness's in-process SMTP sink; the port is written at boot.
    "smtp_host": "127.0.0.1",
    "smtp_use_tls": "false",
    "smtp_from": "fmtk@example.com",
    "state_dir": str(STATE_DIR / "klangk"),
    "data_dir": str(STATE_DIR / "klangk/data"),
}

# klangkd.yaml key -> /api/v1/config key is identity for every key the
# harness swaps today (login_banner_title etc.); wait_config_reflects
# relies on that identity and refuses keys /config does not expose.

VM_URI_TIMEOUT = 600
HTTP_TIMEOUT = 15


class FmtkError(RuntimeError):
    """A fmtk CLI call returned ok=false (message + details)."""


class HarnessTimeout(FmtkError):
    """A wait_for/boot deadline elapsed."""


def http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def http_api(base: str, token: str, method: str, path: str, body=None):
    """One JSON API call; returns (status, parsed-json-or-text)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def http_download(base: str, token: str, path: str) -> bytes:
    """Raw-bytes authenticated GET (exports are tar.gz streams, not JSON)."""
    req = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def http_login(base: str, email: str, password: str) -> str:
    """POST /auth/login; returns the access token (raises on failure)."""
    status, body = http_api(
        base,
        "",
        "POST",
        "/api/v1/auth/login",
        {"identifier": email, "password": password},
    )
    if status != 200:
        raise FmtkError(f"http login as {email} failed ({status}): {body}")
    return body["access_token"]


def wait_http(url: str, name: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http_get_json(url)
            return
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1)
    raise HarnessTimeout(f"timed out waiting for {name} at {url}")


def pids_matching(pattern: str) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    ).stdout
    return [int(p) for p in out.split() if p.strip()]


def config_yaml_path() -> Path:
    return STATE_DIR / "klangkd.yaml"


def read_config_yaml() -> dict:
    """Parse the scratch config (yaml.safe_load — the file fmtk-up.sh
    writes carries inline comments a line-based parse would corrupt)."""
    return yaml.safe_load(config_yaml_path().read_text()) or {}


def write_config_yaml(cfg: dict) -> None:
    """Write the scratch config back (order-preserving)."""
    config_yaml_path().write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)
    )


class SmtpSink:
    """Minimal in-process SMTP capture server (stdio asyncio; no deps).

    klangkd's emailsvc (aiosmtplib) needs a real SMTP peer to hand the
    verification / reset / invitation tokens to. The sink accepts any
    dialogue shape aiosmtplib sends, stores every DATA payload, and the
    tests fish ``#/route?token=...`` URLs back out — email-token flows
    without an SMTP dependency.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._server: asyncio.Server | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> int:
        """Serve on an ephemeral localhost port (daemon thread); the port."""
        started = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, args=(started,), daemon=True
        )
        self._thread.start()
        if not started.wait(10):
            raise FmtkError("SMTP sink did not start within 10s")
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    def _serve(self, started: threading.Event) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)

        async def run() -> None:
            self._server = await asyncio.start_server(
                self._handle_client, "127.0.0.1", 0
            )
            started.set()
            async with self._server:
                await self._server.serve_forever()

        self._loop.run_until_complete(run())

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(b"220 fmtk-sink ESMTP\r\n")
        while True:
            line = await reader.readline()
            if not line:
                break
            upper = line.strip().upper()
            if upper.startswith(b"DATA"):
                # 354 for the command (the body follows), 250 only after
                # the terminating dot — aiosmtplib fails the whole send
                # otherwise, which rolls back e.g. registration
                writer.write(b"354 end with <CR><LF>.<CR><LF>\r\n")
                await writer.drain()
                self.messages.append(await self._read_body(reader))
                writer.write(b"250 ok\r\n")
            elif upper.startswith(b"QUIT"):
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 ok\r\n")
            await writer.drain()
        writer.close()

    async def _read_body(self, reader: asyncio.StreamReader) -> str:
        lines: list[bytes] = []
        while True:
            line = await reader.readline()
            if line.rstrip(b"\r\n") == b".":
                return b"".join(lines).decode(errors="replace")
            lines.append(line)

    def wait_for_message(self, needle: str, timeout: float = 30) -> str:
        """Block until a captured message contains ``needle`` (latest
        wins — a re-sent token supersedes earlier ones)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in reversed(self.messages):
                if needle in message:
                    return message
            time.sleep(1)
        raise HarnessTimeout(f"no captured email contains {needle!r}")

    def token_for(self, route: str, needle: str) -> str:
        """The ``#/route?token=...`` token from the message with ``needle``."""
        message = self.wait_for_message(needle)
        # The text part is quoted-printable: ``=`` encodes as ``=3D`` and
        # long lines soft-wrap with ``=`` + newline mid-token — decode
        # the whole body before extracting, or the token carries an
        # ``3D`` prefix and the server rejects it as invalid.
        body = quopri.decodestring(message.encode("utf-8", "replace")).decode(
            errors="replace"
        )
        match = re.search(rf"#/{route}\?token=([A-Za-z0-9_.\-]+)", body)
        if not match:
            raise FmtkError(f"no #/{route}?token link in message: {body!r}")
        return match.group(1)


class Backend:
    """Scratch klangkd lifecycle: launch/adopt, config swap, SIGHUP, restart."""

    def __init__(self) -> None:
        self.config: dict[str, object] = {}
        self.log_path = STATE_DIR / "klangkd.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{BACKEND_PORT}"

    def pgrep_pattern(self) -> str:
        return f"klangk.main --config {config_yaml_path()}"

    def pids(self) -> list[int]:
        return pids_matching(self.pgrep_pattern())

    def is_ours_and_healthy(self) -> bool:
        if not self.pids():
            return False
        try:
            http_get_json(f"{self.url}/api/v1/config")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure(self) -> None:
        """Adopt a healthy scratch backend or launch a fresh one."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if self.is_ours_and_healthy():
            self.config = read_config_yaml()
            return
        if ports_busy([BACKEND_PORT, EGRESS_PORT]):
            raise FmtkError(
                f"port {BACKEND_PORT}/{EGRESS_PORT} in use by something else — "
                "run fmtk-down or override FMTK_*_PORT"
            )
        self.config = dict(DEFAULT_CONFIG)
        if config_yaml_path().exists():
            self.config = read_config_yaml()
        self.launch()

    def launch(self) -> None:
        write_config_yaml(self.config)
        env = backend_env()
        log = open(self.log_path, "ab")
        subprocess.Popen(
            [
                str(VENV_PYTHON),
                "-m",
                "klangk.main",
                "--config",
                str(config_yaml_path()),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        wait_http(f"{self.url}/api/v1/config", "scratch klangkd", 240)

    def api_config(self) -> dict:
        return http_get_json(f"{self.url}/api/v1/config")

    def sighup(self) -> None:
        """Reload configuration in place (#1587): listener + DB stay up."""
        pids = self.pids()
        if not pids:
            raise FmtkError("scratch klangkd is not running (SIGHUP target gone)")
        os.kill(pids[0], signal.SIGHUP)

    def wait_healthy(self, timeout: float = 240) -> None:
        wait_http(f"{self.url}/api/v1/config", "scratch klangkd", timeout)

    def restart(self) -> None:
        """Stop the scratch klangkd and relaunch it (same state + config)."""
        pids = self.pids()
        if pids:
            os.kill(pids[0], signal.SIGTERM)
            self.wait_gone(pids[0], 30)
        self.launch()

    def wait_gone(self, pid: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pid not in self.pids():
                return
            time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # exited between the poll and the kill

    def swap_settings(
        self,
        changes: dict,
        apply: str = "sighup",
        timeout: float = 120,
        verify: bool = True,
    ) -> dict:
        """Rewrite config keys and make the server pick them up.

        ``apply``: "sighup" (reloadable settings — in-place reload),
        "restart" (deploy-time settings — full process restart), or
        "none" (write only — nothing is applied to the running server,
        so nothing is awaited; the next boot reads the file).
        ``verify``: block on /api/v1/config reflecting the change — pass
        False for keys the endpoint does not expose (jwt_secret,
        step_up_window_minutes, …); those swaps are asserted behaviorally
        by the test instead. Returns the current /api/v1/config payload.
        """
        self.config.update(changes)
        write_config_yaml(self.config)
        if apply == "sighup":
            self.sighup()
        elif apply == "restart":
            self.restart()
        else:
            return self.api_config()
        self.wait_healthy()
        if verify:
            self.wait_config_reflects(changes, timeout)
        return self.api_config()

    def wait_config_value(self, key: str, value, timeout: float = 30) -> None:
        """Block until /api/v1/config carries ``key == value``.

        A SIGHUP reload completes asynchronously — swap_settings can
        return while the old settings are still being served, so a test
        that re-mounts a page right after a swap would let the page's
        one-shot config fetch read the stale value. Gate on the endpoint
        before driving the UI.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.api_config().get(key) == value:
                return
            time.sleep(0.5)
        raise HarnessTimeout(f"/api/v1/config[{key}] never became {value!r}")

    def wait_config_reflects(self, changes: dict, timeout: float) -> None:
        """Block until /api/v1/config carries the swapped values.

        Every swapped key must appear in the payload — a key the endpoint
        never exposes (e.g. jwt_secret) cannot be verified, and silently
        passing on it would be a false "landed" signal, so it raises
        instead. Note this is a settings-swap barrier, not a guarantee
        that every subsystem has reconfigured against the new value.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cfg = self.api_config()
            missing = [key for key in changes if key not in cfg]
            if missing:
                raise FmtkError(
                    f"/api/v1/config does not expose {missing} — the swap "
                    "cannot be verified; assert it another way"
                )
            stale = {
                key: (cfg[key], value)
                for key, value in changes.items()
                if cfg[key] != value
            }
            if not stale:
                return
            time.sleep(1)
        raise HarnessTimeout(f"/api/v1/config never reflected {changes}")


def backend_env() -> dict:
    """Hermetic env for the scratch klangkd: ambient KLANGKD_* is
    scrubbed (env beats the yaml file in settings resolution — a stray
    variable would silently override the config the harness writes,
    #1526), and on stock CI runners system podman (SUID newuidmap) is
    preferred over the nix one on PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("KLANGKD_")}
    if Path("/usr/bin/podman").exists():
        env["KLANGKD_PODMAN_BIN"] = "/usr/bin/podman"
    return env


def ports_busy(ports: list[str]) -> bool:
    out = subprocess.run(["ss", "-tln"], capture_output=True, text=True).stdout
    return any(f":{port} " in line for port in ports for line in out.splitlines())


def port_holders(port: str) -> list[int]:
    """PIDs listening on ``port`` (same-user sockets; ss -tlnp)."""
    out = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True).stdout
    holders: list[int] = []
    for line in out.splitlines():
        if f":{port} " not in line:
            continue
        holders.extend(int(pid) for pid in re.findall(r"pid=(\d+)", line))
    return holders


class Proxy:
    """The origin-splitting caddy fmtk-up fronts the debug run with."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.config_path = STATE_DIR / "proxy.Caddyfile"

    @property
    def pattern(self) -> str:
        return f"caddy run --config {self.config_path}"

    def is_ours_and_healthy(self) -> bool:
        if not pids_matching(self.pattern):
            return False
        try:
            http_get_json(f"http://127.0.0.1:{PROXY_PORT}/api/v1/config")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def ensure(self) -> None:
        if self.is_ours_and_healthy():
            return
        if ports_busy([PROXY_PORT]):
            raise FmtkError(f"port {PROXY_PORT} in use — run fmtk-down")
        self.config_path.write_text(
            f"http://:{PROXY_PORT} {{\n"
            "\tbind 127.0.0.1\n"
            "\thandle /api/* {\n"
            f"\t\treverse_proxy 127.0.0.1:{BACKEND_PORT}\n"
            "\t}\n"
            "\thandle /ws {\n"
            f"\t\treverse_proxy 127.0.0.1:{BACKEND_PORT}\n"
            "\t}\n"
            "\thandle {\n"
            f"\t\treverse_proxy 127.0.0.1:{FLUTTER_PORT}\n"
            "\t}\n"
            "}\n"
        )
        log = open(STATE_DIR / "caddy.log", "ab")
        subprocess.Popen(
            [
                "caddy",
                "run",
                "--config",
                str(self.config_path),
                "--adapter",
                "caddyfile",
            ],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        wait_http(f"http://127.0.0.1:{PROXY_PORT}/api/v1/config", "caddy proxy", 30)


def seed(url: str) -> None:
    """Idempotently apply the fmtk fixture (users + fmtk-verify workspace)."""
    subprocess.run(
        [str(VENV_PYTHON), str(REPO_ROOT / "scripts/fmtk-seed.py"), "--url", url],
        cwd=REPO_ROOT,
        check=True,
    )


def headless_requested() -> bool:
    forced = os.environ.get("FMTK_E2E_HEADLESS", "").lower()
    if forced in ("1", "true", "yes"):
        return True
    if forced in ("0", "false", "no"):
        return False
    return bool(os.environ.get("CI")) or not os.environ.get("DISPLAY")


class FlutterRun:
    """``flutter run --debug -d chrome`` + VM-service/CDP discovery."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.log_path = STATE_DIR / "flutter-e2e.log"
        self.vm_uri = ""
        self.log_offset = 0
        self.launch_serial = 0

    def chrome_env(self) -> dict:
        env = dict(os.environ)
        env["CHROME_EXECUTABLE"] = str(REPO_ROOT / "scripts/fmtk-chrome.sh")
        if headless_requested():
            env["FMTK_CHROME_FLAGS"] = (
                "--headless=new --no-sandbox --disable-gpu "
                "--disable-dev-shm-usage --window-size=1600,1000"
            )
        return env

    def flutter_args(self, url_suffix: str = "") -> list[str]:
        args = [
            "run",
            "--debug",
            "-d",
            "chrome",
            "--web-hostname",
            "127.0.0.1",
            "--web-port",
            FLUTTER_PORT,
        ]
        # Boot the app AT a URL (the fmtk-chrome.sh wrapper rewrites the
        # dev-server origin to the proxy and preserves the path/hash):
        # the pre-auth deep-link UX — e.g. /#/settings — where the router
        # redirects at boot and the login page renders with the
        # pending-redirect message (#3233).
        if url_suffix:
            args += [
                "--web-launch-url",
                f"http://127.0.0.1:{FLUTTER_PORT}{url_suffix}",
            ]
        pkg_config = REPO_ROOT / "src/frontend/.dart_tool/package_config.json"
        lock = REPO_ROOT / "src/frontend/pubspec.lock"
        if pkg_config.exists() and not newer(pkg_config, lock):
            args.append("--no-pub")
        return args

    def launch(self, url_suffix: str = "") -> None:
        if port_holders(FLUTTER_PORT):
            raise FmtkError(
                f"port {FLUTTER_PORT} is already in use — a leftover dev "
                "server would make this launch retry the bind forever; "
                "stop it first (stop()/wipe())"
            )
        self.vm_uri = ""
        # Append (restart_app must not truncate earlier phases out of the CI
        # artifact), and remember where this launch's output starts —
        # scanning the whole file would match the PREVIOUS run's VM URI.
        self.log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            ["flutter", *self.flutter_args(url_suffix)],
            cwd=REPO_ROOT / "src/frontend",
            env=self.chrome_env(),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            self.wait_vm_uri()
        except FmtkError:
            self.stop()  # a failed boot must not leak the flutter process
            raise
        self.wait_toolkit()

    def adopt(self) -> bool:
        """Adopt a still-healthy app from a previous run (fast re-runs).

        The dev server holds our port and the log carries the last VM
        URI; a toolkit ping proves the app answers. Anything else (dead
        app, bind-failed leftover) returns False so the caller stops and
        relaunches.
        """
        if not self.log_path.exists() or not port_holders(FLUTTER_PORT):
            return False
        data = self.log_path.read_bytes()
        match = re.search(rb"ws://\S+/ws", data)
        if not match:
            return False
        self.vm_uri = match.group(0).decode()
        self.log_offset = len(data)
        self.proc = None
        try:
            self.wait_toolkit(timeout=15)
            return True
        except HarnessTimeout:
            self.vm_uri = ""
            return False

    def wait_toolkit(self, timeout: float = 90) -> None:
        """Block until the toolkit answers (dwds isolate attached).

        The VM URI appears in the log before dwds attaches the app
        isolate — driving any earlier gets "No Flutter isolate found"
        (the first navigate of a session reliably loses that race).
        """
        client = FmtkClient(self)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                client.exec("get_app_errors", {"count": 1})
                return
            except FmtkError:
                time.sleep(2)
        raise HarnessTimeout("fmtk toolkit never answered after boot")

    def wait_vm_uri(self) -> None:
        deadline = time.monotonic() + VM_URI_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise FmtkError(
                    f"flutter run exited before exposing a VM service — "
                    f"tail of {self.log_path}:\n{self.tail()}"
                )
            if self.log_path.exists():
                # Slice BYTES, then decode: stat().st_size is bytes, but
                # flutter's output carries multi-byte chars (emoji, box
                # drawing), so a str slice at a byte offset lands past
                # the end and never matches — the silent multi-minute
                # stall this guard replaced.
                fresh = self.log_path.read_bytes()[self.log_offset :].decode(
                    errors="replace"
                )
                match = re.search(r"ws://\S+/ws", fresh)
                if match:
                    self.vm_uri = match.group(0)
                    return
                # A failed compile leaves the flutter PROCESS alive
                # (retrying) — without this check the 600s deadline is
                # the only way out.
                if re.search(r"Failed to compile|^\S+\.dart:\d+:.*Error", fresh):
                    raise FmtkError(
                        "flutter run failed to compile — log tail:\n"
                        + "\n".join(fresh.splitlines()[-15:])
                    )
            time.sleep(2)
        raise HarnessTimeout(
            f"no Dart VM Service in {self.log_path} after {VM_URI_TIMEOUT}s"
        )

    def tail(self, lines: int = 30) -> str:
        if not self.log_path.exists():
            return "(no log)"
        return "\n".join(
            self.log_path.read_text(errors="replace").splitlines()[-lines:]
        )

    def stop_chrome(self) -> None:
        """Close the Chrome window(s) this run opened — even when the
        app beneath is wedged. Matched on the proxy-origin URL the
        fmtk-chrome.sh wrapper rewrites into Chrome's command line,
        plus the run's remote-debugging port; TERM, grace, KILL."""
        patterns = [f"[c]hrome.*127.0.0.1:{PROXY_PORT}"]
        cdp = self.cdp_port()
        if cdp:
            patterns.append(f"[c]hrome.*remote-debugging-port={cdp}")
        pids = {pid for pattern in patterns for pid in pids_matching(pattern)}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pids:
            live = {pid for pattern in patterns for pid in pids_matching(pattern)}
            pids &= live
            if not pids:
                return
            time.sleep(0.5)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cdp_port(self) -> str:
        out = subprocess.run(
            ["pgrep", "-af", "chrome"], capture_output=True, text=True
        ).stdout
        for line in out.splitlines():
            if "remote-debugging-port=" in line:
                return line.split("remote-debugging-port=")[1].split()[0]
        return ""

    def stop(self) -> None:
        # Close the browser window FIRST: a wedged app (dead isolate,
        # hung boot) can keep flutter run from tearing its Chrome down,
        # and a leftover window outlives every failure mode below.
        self.stop_chrome()
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        # The dev server's port must be free before the next launch: the
        # flutter tool daemon can outlive its process group still holding
        # the socket, and a replacement launch then retries the bind
        # forever with no output — a silent 10-minute stall. Kill whoever
        # holds the port (ours by construction: our port override) and
        # fail loudly if it never drains.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            holders = port_holders(FLUTTER_PORT)
            if not holders:
                return
            for pid in holders:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.5)
        raise FmtkError(f"port {FLUTTER_PORT} still in use after stopping flutter run")


def newer(a: Path, b: Path) -> bool:
    return a.stat().st_mtime > b.stat().st_mtime


class FmtkClient:
    """Thin Python wrapper over ``fmtk exec`` against one running app.

    Holds the live :class:`FlutterRun` (not a snapshot URI) so it keeps
    working across ``Harness.restart_app()`` relaunches.
    """

    def __init__(self, flutter: "FlutterRun") -> None:
        self.flutter = flutter
        self.nav_serial = 0

    @property
    def vm_uri(self) -> str:
        uri = self.flutter.vm_uri
        if not uri:
            raise FmtkError("flutter run not launched")
        return uri

    def exec(self, name: str, args: dict | None = None) -> object:
        proc = subprocess.run(
            [
                "fmtk",
                "exec",
                "--name",
                name,
                "--vm-service-uri",
                self.vm_uri,
                "--args",
                json.dumps(args or {}),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        envelope = self.parse_envelope(proc.stdout, name, proc.stderr)
        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            raise FmtkError(
                f"fmtk {name} failed: {err.get('message')} ({err.get('details')})"
            )
        return envelope.get("data")

    def parse_envelope(self, stdout: str, name: str, stderr: str) -> dict:
        try:
            return json.loads(stdout)
        except ValueError:
            raise FmtkError(
                f"fmtk {name} printed no JSON envelope (stderr: {stderr[-500:]}; "
                f"stdout: {stdout[-500:]})"
            ) from None

    # --- snapshot / interaction helpers ---------------------------------

    def snapshot(self) -> object:
        return self.exec("semantic_snapshot")

    def wait_for_text(self, text: str, timeout_ms: int = 30000) -> object:
        """Block until ``text`` appears anywhere in the semantic tree.

        fmtk caps one wait_for at 30s (schema max), and the first debug
        frame after dwds attach can take longer — so this loops 25s
        slices until the outer deadline elapses.
        """
        deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            slice_ms = min(25000, max(remaining_ms, 1000))
            try:
                return self.exec(
                    "wait_for",
                    {
                        "predicate": {"kind": "text", "text": text},
                        "timeoutMs": slice_ms,
                    },
                )
            except FmtkError:
                if time.monotonic() >= deadline:
                    raise

    def has_text(self, text: str, timeout_ms: int = 2000) -> bool:
        try:
            self.wait_for_text(text, timeout_ms)
            return True
        except FmtkError:
            return False

    def wait_gone(self, text: str, timeout: float = 10) -> None:
        """Block until ``text`` leaves the tree. has_text only waits for
        APPEARANCE — asserting a config swap's effect on already-rendered
        UI must wait for the removal instead."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.has_text(text, 500):
                return
            time.sleep(0.5)
        raise HarnessTimeout(f"{text!r} never left the semantic tree")

    def tap(self, ref: str) -> None:
        """Tap a ref; a structured failure (e.g. web_gesture_not_supported
        for a node without the tap action) must fail the test, not no-op."""
        result = self.exec("tap_widget", {"ref": ref})
        if isinstance(result, dict) and result.get("success") is False:
            raise FmtkError(f"tap_widget failed on {ref}: {result}")

    def enter_text(self, ref: str, text: str) -> None:
        self.exec("enter_text", {"ref": ref, "text": text})

    def tap_label(self, label: str, node_kind: str = "button") -> None:
        """Tap the first ``node_kind`` (button by default) whose labels
        contain ``label`` — the button filter matters because a page's
        heading text often duplicates the button label ("Log In")."""
        self.tap(self.ref_for_label(label, node_kind))

    def ref_for_label(self, label: str, node_kind: str | None = None) -> str:
        def matches(node: dict) -> bool:
            # substring match: merged semantics routinely double labels
            # ('Invitations\nInvitations' — a wrapping Semantics label plus
            # the child Text), and exact equality would never find them
            if not any(label in str(entry) for entry in node_labels(node)):
                return False
            return node_kind is None or node_type(node) == node_kind

        nodes = find_nodes(self.snapshot(), matches)
        if not nodes:
            raise FmtkError(f"no snapshot node labeled {label!r}")
        return nodes[0]["ref"]

    def login(self, email: str, password: str, expect_text: str) -> None:
        """Fill the login form and submit; wait for ``expect_text``.

        A configured login banner renders as a consent dialog (Cancel /
        I Accept) over the form — dismiss it first when present.
        """
        if self.has_text(expect_text, 2000):
            return  # already logged in (persisted token after hot restart)
        self.dismiss_login_banner()
        fields = find_nodes(self.snapshot(), lambda n: node_type(n) == "textField")
        if len(fields) < 2:
            raise FmtkError(f"login form not visible (fields found: {len(fields)})")
        self.enter_text(fields[0]["ref"], email)
        self.enter_text(fields[1]["ref"], password)
        self.tap_label("Log In")
        self.wait_for_text(expect_text)

    def logout(self) -> None:
        """Tap the app-bar logout icon (its tooltip is the semantic
        label; icon-only fallback: the rightmost button — logout is the
        last app-bar action) and wait for the login surface."""
        if self.has_text("Log In", 2000):
            return  # already logged out
        try:
            self.tap_label("Logout")
        except FmtkError:
            self.tap_rightmost_button()
        self.wait_for_login_page()

    def tap_rightmost_button(self) -> None:
        """Tap the button with the greatest right edge (ties: topmost).

        The app-bar's actions sit at the far right edge of the top bar,
        so the rightmost button overall is its last icon (logout).
        """
        self.tap_button_from_right(0)

    def tap_identifier(self, identifier: str) -> None:
        """Tap the node carrying ``identifier`` — or, when the identifier
        wraps a control (Semantics container), the tappable node inside
        it. Identifiers are the deterministic locator for instrumented
        widgets (``Semantics(identifier: ...)``) where labels do not
        merge (FABs, dialogs)."""
        nodes = find_nodes(self.snapshot(), lambda n: n.get("identifier") == identifier)
        if not nodes:
            raise FmtkError(f"no snapshot node with identifier {identifier!r}")
        target = nodes[0]
        if "tap" in (target.get("actions") or []):
            self.tap(target["ref"])
            return
        descendants = self.descendants_with_tap(target)
        if not descendants:
            raise FmtkError(f"node {identifier!r} has no tappable descendant")
        self.tap(descendants[0]["ref"])

    def descendants_with_tap(self, node: dict) -> list[dict]:
        found: list[dict] = []
        refs = {c["ref"] for c in (node.get("children") or [])}
        if not refs:
            return found
        candidates = find_nodes(self.snapshot(), lambda n: n.get("ref") in refs)
        for candidate in candidates:
            if "tap" in (candidate.get("actions") or []):
                found.append(candidate)
            found.extend(self.descendants_with_tap(candidate))
        return found

    def enter_text_identifier(self, identifier: str, text: str) -> None:
        """Type into the text field at/below the identifier node."""
        nodes = find_nodes(self.snapshot(), lambda n: n.get("identifier") == identifier)
        if not nodes:
            raise FmtkError(f"no snapshot node with identifier {identifier!r}")
        fields = find_nodes(
            self.snapshot(),
            lambda n: (
                node_type(n) == "textField"
                and (
                    n.get("ref") == nodes[0].get("ref")
                    or self.is_descendant(n, nodes[0])
                )
            ),
        )
        if not fields:
            raise FmtkError(f"no text field under identifier {identifier!r}")
        self.enter_text(fields[0]["ref"], text)

    def is_descendant(self, node: dict, ancestor: dict) -> bool:
        parent_refs = {c["ref"] for c in (ancestor.get("children") or [])}
        if node.get("ref") in parent_refs:
            return True
        return any(
            self.is_descendant(node, candidate)
            for candidate in find_nodes(
                self.snapshot(), lambda n: n.get("ref") in parent_refs
            )
        )

    def tap_lowest_button(self) -> None:
        """Tap the lowest button in the screen's right edge (a corner FAB).

        FloatingActionButton semantics never merge child labels — icon
        semanticLabels and Semantics wrappers alike — so corner FABs are
        addressed positionally. Constrained to the right edge because
        long lists put row buttons below the FAB's top.
        """
        buttons = find_nodes(self.snapshot(), lambda n: node_type(n) == "button")
        if not buttons:
            raise FmtkError("no buttons visible for tap_lowest_button")
        right_edge = max((n.get("bounds") or {}).get("right", 0) for n in buttons)
        corner = [
            n
            for n in buttons
            if (n.get("bounds") or {}).get("right", 0) >= right_edge - 160
            and "tap" in (n.get("actions") or [])
        ]
        if not corner:
            raise FmtkError("no tappable corner button found")
        target = max(corner, key=lambda n: (n.get("bounds") or {}).get("top", -1))
        self.tap(target["ref"])

    def tap_button_from_right(self, index: int) -> None:
        """Tap the app-bar button ``index`` places from the right edge.

        Icon-only app-bar actions carry no semantic label (tooltips are
        not exposed), so they are addressed positionally among the
        topmost buttons: 0 = logout (rightmost), 1 = admin, …
        """
        buttons = find_nodes(self.snapshot(), lambda n: node_type(n) == "button")
        if not buttons:
            raise FmtkError("no buttons visible for tap_button_from_right")
        top = min((n.get("bounds") or {}).get("top", 0) for n in buttons)
        bar = [n for n in buttons if (n.get("bounds") or {}).get("top", 0) <= top + 120]
        if len(bar) <= index:
            raise FmtkError(f"app-bar has {len(bar)} buttons, need index {index}")
        target = max(bar, key=lambda n: (n.get("bounds") or {}).get("right", -1))
        for _ in range(index):
            bar.remove(target)
            target = max(bar, key=lambda n: (n.get("bounds") or {}).get("right", -1))
        self.tap(target["ref"])

    # --- label / identifier locators (#3234) -----------------------------

    def tap_labeled_exact(self, label: str) -> None:
        """Tap the nearest tappable node at or above the node whose label
        carries ``label`` as a whole line.

        A list tile's title text is not itself tappable (the tap lives on
        the tile semantics above it), and substring matching would hit
        the tile's own ``Delete <name>`` trailing button — so the label
        match is line-exact and the climb starts at the labeled node.
        Tabs, segments and tiles all resolve through this.
        """
        tree = self.snapshot()
        hits = find_label_nodes(tree, label, exact=True)
        if not hits:
            raise FmtkError(f"no snapshot node labeled {label!r}")
        parents = parent_map(tree)
        node: dict | None = hits[0]
        while node is not None:
            if "tap" in (node.get("actions") or []):
                return self.tap(node["ref"])
            node = parents.get(id(node))
        raise FmtkError(f"no tappable node above label {label!r}")

    def tap_button_exact(self, label: str) -> None:
        """Tap the button whose labels carry ``label`` as a whole line.

        Button semantics absorb Semantics-wrapper identifiers (only
        text fields keep them), and substring matching collides with
        siblings ('Restart' vs 'Restart now', 'Shut Down' vs 'Shut Down
        Container') — exact-line matching is the deterministic form.
        """
        hits = [
            node
            for node in find_nodes(self.snapshot(), lambda n: node_type(n) == "button")
            if label_has_line(node, label)
        ]
        if not hits:
            raise FmtkError(f"no button labeled {label!r}")
        self.tap(hits[0]["ref"])

    def wait_for_label(self, text: str, timeout: float = 30) -> dict:
        """Block until some node's label fields carry ``text``.

        Semantic labels (icon ``semanticLabel``s, ``Semantics(label:)``
        wrappers) are not text-kind nodes, so ``wait_for``'s text
        predicate cannot see them — e.g. the list tile's
        ``Workspace status:`` state.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = find_label_nodes(self.snapshot(), text)
            if hits:
                return hits[0]
            time.sleep(1)
        raise HarnessTimeout(f"no node label carries {text!r}")

    def identifier_node(self, identifier: str) -> dict | None:
        hits = find_nodes(self.snapshot(), lambda n: n.get("identifier") == identifier)
        return hits[0] if hits else None

    def wait_for_identifier(self, identifier: str, timeout: float = 30) -> dict:
        """Block until the instrumented node (``Semantics(identifier:)``)
        is in the tree."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            node = self.identifier_node(identifier)
            if node:
                return node
            time.sleep(1)
        raise HarnessTimeout(f"no node with identifier {identifier!r}")

    def wait_identifier_gone(self, identifier: str, timeout: float = 60) -> None:
        """Block until the instrumented node leaves the tree (an overlay
        the server cleared, a dialog that closed)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.identifier_node(identifier) is None:
                return
            time.sleep(1)
        raise HarnessTimeout(f"identifier {identifier!r} never left the tree")

    def scroll_until_label(self, text: str, timeout: float = 60) -> None:
        """Scroll down until ``text`` is labeled by a node IN the
        viewport — the snapshot lists off-screen nodes too, so a bare
        label match can leave the target untappable."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            visible = [
                node
                for node in find_label_nodes(self.snapshot(), text)
                if node.get("visibleInViewport")
            ]
            if visible:
                return
            self.exec("scroll", {"direction": "down", "distance": 600})
            time.sleep(0.5)
        raise HarnessTimeout(f"{text!r} never appeared while scrolling")

    # --- hash-route navigation + in-app evaluation (per AGENTS.md) ------

    def navigate(self, path: str) -> None:
        """Hash-route the driven tab to ``#/path`` via navigateTo.

        Only the hash differs, so the app keeps running — Flutter's hash
        browser-history listener hands the new location to GoRouter. This
        is how email-token links (verify / reset / accept-invite) reach
        their pages without a page reload (evaluate cannot import GoRouter
        itself; navigateTo is in scope from the login page's library).

        A full-page navigation is NOT usable here: reloading the tab kills
        the dwds isolate and fmtk loses the app — to observe boot-time
        effects (pre-auth deep links), use ``Harness.restart_app(at_path=)``.
        """
        escaped = path.replace("\\", "\\\\").replace("'", "\\'")
        self.exec(
            "evaluate_dart_expression",
            {
                "expression": (
                    f"navigateTo('http://127.0.0.1:{PROXY_PORT}/#{escaped}')"
                ),
                "libraryUri": "package:klangk_frontend/auth/login_page.dart",
            },
        )

    AUTH_LIBRARY = "package:klangk_frontend/app.dart"
    AUTH_WALK_TEMPLATE = """
() {{
  AuthService? auth;
  void walk(Element el) {{
    if (auth != null) return;
    if (el is StatefulElement) {{
      try {{
        auth = Provider.of<AuthService>(el, listen: false);
        return;
      }} catch (_) {{}}
    }}
    el.visitChildElements((c) => walk(c));
  }}
  walk(WidgetsBinding.instance.rootElement!);
  if (auth == null) return 'NO-AUTH';
  {body}
}}()
"""

    def auth_eval(self, body: str) -> object:
        """Evaluate against the app's live :class:`AuthService`.

        Walks the tree for any stateful element the MultiProvider
        actually covers (the root View element sits ABOVE it and must be
        skipped; Provider.of from a covered element never throws) and
        runs ``body`` with ``auth!`` bound to the service. ``app.dart``'s
        library scope carries provider's ``Provider.of`` plus the
        widgets imports the walk needs. Bodies must be complete
        statements — the evaluator does not append semicolons.
        """
        data = self.exec(
            "evaluate_dart_expression",
            {
                "expression": self.AUTH_WALK_TEMPLATE.format(body=body),
                "libraryUri": self.AUTH_LIBRARY,
            },
        )
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def dismiss_login_banner(self) -> None:
        if self.has_text("I Accept", 3000):
            self.tap_label("I Accept")

    def wait_for_login_page(self, timeout_ms: int = 90000) -> None:
        """The login surface: the form, or the banner dialog covering it."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.has_text("Log In", 2000) or self.has_text(
                "Sign in to continue", 2000
            ):
                return
            time.sleep(1)
        raise HarnessTimeout("login page never appeared")

    def app_errors(self) -> list:
        """Uncaught Flutter errors, minus known framework routing noise."""
        data = self.exec("get_app_errors", {"count": 20})
        errors = data.get("errors", data) if isinstance(data, dict) else data
        return [
            error
            for error in (errors or [])
            if not self.is_framework_noise(str(error.get("message", "")))
        ]

    # Framework artifacts of routing a Router app outside a cold boot —
    # go_router has already routed correctly; these are the Navigator's
    # legacy fallbacks complaining (booting at a deep link: initialRoute;
    # an in-tab hash change: didPushRouteInformation -> pushNamed).
    # Visible only under the debug error monitor; real app errors still
    # fail the drain untouched.
    FRAMEWORK_NOISE = (
        "Could not navigate to initial route",
        "Could not find a generator for route",
    )

    def is_framework_noise(self, message: str) -> bool:
        return any(pattern in message for pattern in self.FRAMEWORK_NOISE)

    def hot_restart(self) -> None:
        """dwds hot restart. Fails with a chrome-devtools error against
        the proxied debug origin in this setup — prefer
        ``Harness.restart_app()`` (fresh boot, deterministic)."""
        self.exec("hot_restart_flutter")

    # --- terminal (canvas: driven via evaluate, per AGENTS.md) -----------

    TERMINAL_LIBRARY = "package:klangk_frontend/terminal/ghostty_terminal.dart"

    WALK_TEMPLATE = """
() {{
  GhosttyTerminalState? st;
  void walk(Element el) {{
    if (st != null) return;
    if (el is StatefulElement && el.state is GhosttyTerminalState) {{
      st = el.state as GhosttyTerminalState;
      return;
    }}
    el.visitChildElements((c) => walk(c));
  }}
  walk(WidgetsBinding.instance.rootElement!);
  if (st == null) return 'NO-TERMINAL-STATE';
  {body}
}}()
"""

    def terminal_eval(self, body: str) -> str:
        result = self.exec(
            "evaluate_dart_expression",
            {
                "expression": self.WALK_TEMPLATE.format(body=body),
                "libraryUri": self.TERMINAL_LIBRARY,
            },
        )
        # the evaluate envelope wraps the value in {'result': ...} — the
        # same unwrap auth_eval applies, or every buffer read comes back
        # as a dict repr
        if isinstance(result, dict) and "result" in result:
            return str(result["result"])
        return str(result)

    def terminal_send(self, text: str) -> str:
        """Type AND execute raw input in the focused terminal. The body
        is a bare statement — the trailing semicolon is required (the
        evaluator does not append one)."""
        escaped = (
            text.replace("\\", "\\\\")
            .replace("$", "\\$")  # $ interpolates in the Dart literal
            .replace("'", "\\'")
            .replace("\n", "\\n")
        )
        return self.terminal_eval(f"st!._terminal.sendText('{escaped}');")

    def terminal_buffer(self) -> str:
        """The visible terminal buffer (plain, unwrapped, trimmed)."""
        return self.terminal_eval(
            "var f = st!._terminal.createFormatter("
            "format: FormatterFormat.plain, unwrap: true, trim: true); "
            "try { return f.format(); } finally { f.dispose(); }"
        )


def node_labels(node: dict) -> list[str]:
    """Every human-string field a snapshot node may carry."""
    keys = ("label", "text", "value", "name", "title", "tooltip", "hint")
    return [str(node[k]) for k in keys if node.get(k)]


def label_has_line(node: dict, text: str) -> bool:
    """The node's labels carry ``text`` as a whole line (merged
    semantics join labels with newlines — line equality stays exact)."""
    for entry in node_labels(node):
        if any(line == text for line in entry.split("\n")):
            return True
    return False


def find_label_nodes(tree, text: str, exact: bool = False) -> list[dict]:
    """Nodes whose label fields carry ``text`` — as a whole line
    (``exact``) or as a substring of any line. Merged semantics join
    labels with newlines, so line-wise matching keeps exact hits exact."""

    def matches(node: dict) -> bool:
        for entry in node_labels(node):
            for line in entry.split("\n"):
                if line == text if exact else text in line:
                    return True
        return False

    return find_nodes(tree, matches)


def parent_map(tree) -> dict:
    """``id(node) -> parent node`` over the snapshot tree — the climb
    from a labeled node to its tappable ancestor needs parent links the
    nodes themselves do not carry."""
    parents: dict = {}

    def link(node, parent) -> None:
        if isinstance(node, dict):
            parents[id(node)] = parent
            for child in node.get("children") or []:
                link(child, node)
        elif isinstance(node, list):
            for child in node:
                link(child, parent)

    link(tree, None)
    return parents


def node_type(node: dict) -> str:
    return str(node.get("type") or node.get("role") or "")


def find_nodes(tree, predicate) -> list[dict]:
    """Depth-first collect of snapshot nodes matching ``predicate``."""
    found: list[dict] = []
    walk_nodes(tree, predicate, found)
    return found


def walk_nodes(node, predicate, found: list) -> None:
    if isinstance(node, dict):
        if "ref" in node and predicate(node):
            found.append(node)
        for child in node.values():
            walk_nodes(child, predicate, found)
    elif isinstance(node, list):
        for child in node:
            walk_nodes(child, predicate, found)


class Harness:
    """One running fmtk stack: backend + proxy + flutter + client."""

    def __init__(self) -> None:
        self.backend = Backend()
        self.proxy = Proxy(self.backend)
        self.flutter = FlutterRun()
        self.smtp = SmtpSink()

    @property
    def client(self) -> FmtkClient:
        if not self.flutter.vm_uri:
            raise FmtkError("flutter run not launched (call boot() first)")
        return FmtkClient(self.flutter)

    def restart_app(self, at_path: str | None = None) -> None:
        """Stop and relaunch the debug app (fresh main() -> config
        re-fetch). The deterministic substitute for dwds hot restart,
        which fails against the proxied debug origin (chrome devtools
        error); the incremental debug rebuild keeps it to ~15-30s.

        ``at_path`` boots the app AT ``#/path`` (the pre-auth deep-link
        UX — a full-page hash navigation inside a running tab kills the
        dwds isolate, so booting there is the only reliable way).

        The app-error monitor is a rolling window that a restart would
        silently clear — drain it first so errors from the outgoing app
        instance fail the test instead of being laundered away."""
        errors = self.client.app_errors()
        if errors:
            raise FmtkError(f"app errors before restart_app: {errors}")
        url_suffix = ""
        if at_path is not None:
            self.flutter.launch_serial += 1
            url_suffix = f"/?e2e_boot={self.flutter.launch_serial}#{at_path}"
        self.flutter.stop()
        self.flutter.launch(url_suffix)

    def boot(self, fresh: bool = False) -> None:
        if fresh:
            self.wipe()
        self.backend.ensure()
        # The sink's port is ephemeral, so the SMTP settings are rewritten
        # on every boot and reloaded over SIGHUP (emailsvc reads live off
        # settings — no restart needed, adopted or fresh).
        self.config["smtp_port"] = str(self.smtp.start())
        # Same for the lockout disable when adopting a backend whose yaml
        # predates it (fresh stacks get it via DEFAULT_CONFIG): a locked
        # admin from earlier runs would fail every login for 15 minutes.
        self.config.setdefault("login_lockout_failures", "0")
        write_config_yaml(self.config)
        self.backend.sighup()
        self.backend.wait_healthy()
        self.proxy.ensure()
        seed(self.backend.url)
        # Adopt a healthy app from a previous run (FMTK_E2E_KEEP_APP=1
        # skips stopping it in teardown) — re-runs then skip the ~90s
        # flutter boot entirely. Anything stale falls back to a fresh
        # launch (stop() drains the port first).
        if not self.flutter.adopt():
            self.flutter.stop()
            self.flutter.launch()

    @property
    def config(self) -> dict:
        return self.backend.config

    def wipe(self) -> None:
        """Stop OUR stack — backend + proxy by config-path-scoped patterns,
        the flutter run and its Chrome by port-scoped patterns using OUR
        port overrides — then delete the scratch state. Like ``fmtk-down
        --wipe`` but safe next to a sibling harness on other ports."""
        self.flutter.stop()
        patterns = [
            self.backend.pgrep_pattern(),
            self.proxy.pattern,
            f"run --debug -d chrome.*--web-port {FLUTTER_PORT}",
            f"[c]hrome.*127.0.0.1:{PROXY_PORT}",
        ]
        for pattern in patterns:
            for pid in pids_matching(pattern):
                os.kill(pid, signal.SIGTERM)
        for port in (FLUTTER_PORT, PROXY_PORT, BACKEND_PORT, EGRESS_PORT):
            for pid in port_holders(port):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(2)
        for pattern in patterns:
            for pid in pids_matching(pattern):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for port in (FLUTTER_PORT, PROXY_PORT, BACKEND_PORT, EGRESS_PORT):
            for pid in port_holders(port):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(STATE_DIR, ignore_errors=True)

    def teardown(self) -> None:
        """Stop the flutter run; keep backend + proxy for fast re-runs.

        FMTK_E2E_KEEP_APP=1 also keeps the app running — the next run
        adopts it (boot() pings it first) instead of paying the boot.
        """
        if os.environ.get("FMTK_E2E_KEEP_APP") != "1":
            self.flutter.stop()

    # --- admin-side setup (HTTP, not UI) --------------------------------

    def admin_api(self, method: str, path: str, body: dict | None = None):
        """One API call as the scratch default admin; (status, json)."""
        token = http_login(
            self.backend.url,
            self.config["default_user"],
            self.config["default_password"],
        )
        return http_api(self.backend.url, token, method, path, body)

    def force_password_change(self, email: str, password: str) -> None:
        """(Re)arm a user for the forced-change flow: admin-set password
        implies ``must_change_password`` (#3172). Idempotent across runs
        — the caller passes a run-unique password, so the history check
        never trips."""
        status, listing = self.admin_api("GET", "/api/v1/users?page_size=100")
        if status != 200:
            raise FmtkError(f"user listing failed ({status}): {listing}")
        user_id = next((u["id"] for u in listing["users"] if u["email"] == email), None)
        if user_id is None:
            raise FmtkError(f"{email} is not seeded")
        status, body = self.admin_api(
            "PATCH", f"/api/v1/users/{user_id}", {"password": password}
        )
        if status != 200:
            raise FmtkError(f"admin password set for {email} failed: {body}")
