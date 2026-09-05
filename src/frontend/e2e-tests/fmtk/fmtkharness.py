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

import json
import os
import re
import shutil
import signal
import subprocess
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
        self, changes: dict, apply: str = "sighup", timeout: float = 120
    ) -> dict:
        """Rewrite config keys and make the server pick them up.

        ``apply``: "sighup" (reloadable settings — in-place reload),
        "restart" (deploy-time settings — full process restart), or
        "none" (write only — nothing is applied to the running server,
        so nothing is awaited; the next boot reads the file). Returns the
        current /api/v1/config payload.
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
        self.wait_config_reflects(changes, timeout)
        return self.api_config()

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

    def chrome_env(self) -> dict:
        env = dict(os.environ)
        env["CHROME_EXECUTABLE"] = str(REPO_ROOT / "scripts/fmtk-chrome.sh")
        if headless_requested():
            env["FMTK_CHROME_FLAGS"] = (
                "--headless=new --no-sandbox --disable-gpu "
                "--disable-dev-shm-usage --window-size=1600,1000"
            )
        return env

    def flutter_args(self) -> list[str]:
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
        pkg_config = REPO_ROOT / "src/frontend/.dart_tool/package_config.json"
        lock = REPO_ROOT / "src/frontend/pubspec.lock"
        if pkg_config.exists() and not newer(pkg_config, lock):
            args.append("--no-pub")
        return args

    def launch(self) -> None:
        self.vm_uri = ""
        # Append (restart_app must not truncate earlier phases out of the CI
        # artifact), and remember where this launch's output starts —
        # scanning the whole file would match the PREVIOUS run's VM URI.
        self.log_offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            ["flutter", *self.flutter_args()],
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

    def wait_vm_uri(self) -> None:
        deadline = time.monotonic() + VM_URI_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise FmtkError(
                    f"flutter run exited before exposing a VM service — "
                    f"tail of {self.log_path}:\n{self.tail()}"
                )
            if self.log_path.exists():
                fresh = self.log_path.read_text(errors="replace")[self.log_offset :]
                match = re.search(r"ws://\S+/ws", fresh)
                if match:
                    self.vm_uri = match.group(0)
                    return
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

    def cdp_port(self) -> str:
        out = subprocess.run(
            ["pgrep", "-af", "chrome"], capture_output=True, text=True
        ).stdout
        for line in out.splitlines():
            if "remote-debugging-port=" in line:
                return line.split("remote-debugging-port=")[1].split()[0]
        return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


def newer(a: Path, b: Path) -> bool:
    return a.stat().st_mtime > b.stat().st_mtime


class FmtkClient:
    """Thin Python wrapper over ``fmtk exec`` against one running app.

    Holds the live :class:`FlutterRun` (not a snapshot URI) so it keeps
    working across ``Harness.restart_app()`` relaunches.
    """

    def __init__(self, flutter: "FlutterRun") -> None:
        self.flutter = flutter

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

    def tap(self, ref: str) -> None:
        self.exec("tap_widget", {"ref": ref})

    def enter_text(self, ref: str, text: str) -> None:
        self.exec("enter_text", {"ref": ref, "text": text})

    def tap_label(self, label: str, node_kind: str = "button") -> None:
        """Tap the first ``node_kind`` (button by default) whose labels
        contain ``label`` — the button filter matters because a page's
        heading text often duplicates the button label ("Log In")."""
        self.tap(self.ref_for_label(label, node_kind))

    def ref_for_label(self, label: str, node_kind: str | None = None) -> str:
        def matches(node: dict) -> bool:
            if label not in node_labels(node):
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
        data = self.exec("get_app_errors", {"count": 20})
        errors = data.get("errors", data) if isinstance(data, dict) else data
        return errors or []

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
        return str(result)

    def terminal_send(self, text: str) -> str:
        """Type AND execute raw input in the focused terminal."""
        escaped = (
            text.replace("\\", "\\\\")
            .replace("$", "\\$")  # $ interpolates in the Dart literal
            .replace("'", "\\'")
            .replace("\n", "\\n")
        )
        return self.terminal_eval(f"st!._terminal.sendText('{escaped}')")

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

    @property
    def client(self) -> FmtkClient:
        if not self.flutter.vm_uri:
            raise FmtkError("flutter run not launched (call boot() first)")
        return FmtkClient(self.flutter)

    def restart_app(self) -> None:
        """Stop and relaunch the debug app (fresh main() -> config
        re-fetch). The deterministic substitute for dwds hot restart,
        which fails against the proxied debug origin (chrome devtools
        error); the incremental debug rebuild keeps it to ~15-30s.

        The app-error monitor is a rolling window that a restart would
        silently clear — drain it first so errors from the outgoing app
        instance fail the test instead of being laundered away."""
        errors = self.client.app_errors()
        if errors:
            raise FmtkError(f"app errors before restart_app: {errors}")
        self.flutter.stop()
        self.flutter.launch()

    def boot(self, fresh: bool = False) -> None:
        if fresh:
            self.wipe()
        self.backend.ensure()
        self.proxy.ensure()
        seed(self.backend.url)
        self.flutter.launch()

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
        time.sleep(2)
        for pattern in patterns:
            for pid in pids_matching(pattern):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        shutil.rmtree(STATE_DIR, ignore_errors=True)

    def teardown(self) -> None:
        """Stop the flutter run; keep backend + proxy for fast re-runs."""
        self.flutter.stop()
