"""fmtk e2e: the terminal surface — own terminals, gating, shared
terminals, buffer assertions (#3235).

The terminal renders on a canvas (flterm), so every scenario drives it
through ``evaluate_dart_expression`` over the GhosttyTerminalState (per
AGENTS.md) and asserts on the **buffer**, never the widget tree:
``sendText`` types AND executes, the plain formatter reads the visible
buffer, and ``st.widget.wsClient`` exposes the live client state
(``terminalWindows``, ``sharedTerminals``, ``containerReady``).
``sendText``'s return proves nothing — only a buffer read does.

Coverage: the code-in-isolation gate (owner/coder/collaborator get own
terminals and the ``+``; the spectator gets neither and typing is
inert), the command round-trip (echo, whoami, exit codes, scrollback),
the own-terminal tab lifecycle (``+`` creates, switching preserves each
window's buffer, close removes), a shared window watched read-only from
the spectator's side, and the ``allow_sudo`` deploy ceiling flipped
over SIGHUP with no re-login (sudo refused → allowed across a container
restart). The readline keymap (Ctrl+A/Ctrl+E) and PageUp copy-mode stay
wire-level in the Playwright keymap spec: fmtk's synthesized ctrl
combos do not route through flterm's key pipeline, and sendText
bypasses it by design.

Scenarios share sessions in definition order: 2-3 drive one
run-registered user's scratch workspace; 4 and 5 are self-contained
fixture logins. Re-run the whole file (``-k`` selections break the
chain — workspace names and users are run-unique).

The fixture workspace ``fmtk-verify`` is never mutated: the gate test
only reads it, the shared-terminal test shares and unshares one window
(verified gone at the end), and the sudo test's scratch workspace is
run-unique and deleted by the scenario that created it.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid

import aiohttp

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    find_label_nodes,
    find_nodes,
    http_api,
    http_login,
    node_type,
)

RUN = uuid.uuid4().hex[:6]
TERM_EMAIL = f"fmtk-term{RUN}@example.com"
TERM_PW = f"fmtk-Term{RUN}!E5"
TABS_WS = f"fmtk-tabs-{RUN}"
SUDO_WS = f"fmtk-sudo-{RUN}"
CODER_EMAIL = "fmtk-coder@example.com"
COLLAB_EMAIL = "fmtk-collaborator@example.com"
SPECTATOR_EMAIL = "fmtk-spectator@example.com"


# --- shared driving helpers (suite-local; harness keeps primitives) ----


def walk_fields(app) -> list[dict]:
    """The visible text fields, in reading order."""
    return find_nodes(app.snapshot(), lambda n: node_type(n) == "textField")


def at_login(harness, app) -> None:
    """Land on the usable login form (dead sessions are ended, a dead
    app restarted), then dismiss any leftover login-banner dialog."""
    app.navigate("/login")
    if not app.has_text("Log In", 10000):
        try:
            app.auth_eval("auth!.logout(); return 'ok';")
        except FmtkError:
            harness.restart_app()
    app.wait_for_login_page()
    app.dismiss_login_banner()
    app.wait_for_text("Email or handle")


def register_user(harness, app, email: str, password: str) -> None:
    """Register + email-verify; the auto-login lands on the empty list."""
    app.tap_label("Need an account? Create one")
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], email)
    app.enter_text(fields[1]["ref"], password)
    app.tap_label("Create Account")
    app.wait_for_text("Check your email to verify your account.")
    token = harness.smtp.token_for("verify", email)
    app.navigate(f"/verify?token={token}")
    app.wait_for_text("No workspaces yet. Create one to get started.")


def create_workspace(app, name: str) -> None:
    """Create via the create FAB + dialog (the #3234-proven labels)."""
    app.tap_label("Create workspace")
    app.wait_for_text("New Workspace")
    app.enter_text_identifier("create-workspace-name", name)
    app.tap_label("Create")
    app.wait_gone("New Workspace")
    app.wait_for_text(name)


def open_fixture_workspace(app, email: str) -> None:
    """Open ``fmtk-verify`` for a fixture member: the owner finds it
    under Owned by Me, role members tap the Shared with Me segment
    first. The workspace page's Terminal tab is the mount signal. The
    tile is addressed as a button (shared tiles surface no tap actions,
    so the exact-label climb finds nothing to tap)."""
    app.navigate("/workspaces")
    if email != ADMIN_EMAIL:
        app.tap_labeled_exact("Shared with Me")
    app.wait_for_text("fmtk-verify")
    app.tap_button_exact("fmtk-verify")
    app.wait_for_text("Terminal", 60000)
    app.tap_labeled_exact("Terminal")


def open_scratch_workspace(app, name: str) -> None:
    """Open a scratch workspace tile robustly: scroll the tile into the
    viewport, tap it as a button, and retry the whole open — a tap can
    land while the list is still settling after a login."""
    for _ in range(3):
        app.navigate("/workspaces")
        app.wait_for_text(name)
        app.scroll_until_label(name)
        app.tap_button_exact(name)
        try:
            app.wait_for_text("Terminal", 30000)
            app.tap_labeled_exact("Terminal")
            return
        except FmtkError:
            continue
    raise FmtkError(f"{name} never opened")


def delete_from_list(app, name: str) -> None:
    """Delete via the tile's trailing button + confirm dialog."""
    app.navigate("/workspaces")
    app.wait_for_text(name)
    app.tap_label(f"Delete {name}")
    app.wait_for_text("This will delete the workspace")
    app.tap_button_exact("Delete")
    app.wait_gone(name)


def terminal_windows(app) -> list[dict]:
    """The client's live own-window list (jsonEncode over the eval)."""
    raw = app.terminal_eval("return jsonEncode(st!.widget.wsClient.terminalWindows);")
    return json.loads(raw)


def shared_terminals(app) -> list[dict]:
    raw = app.terminal_eval("return jsonEncode(st!.widget.wsClient.sharedTerminals);")
    return json.loads(raw)


def wait_terminal_windows(app, count: int, timeout: float = 90) -> list[dict]:
    """Poll until the own-window list reaches ``count`` entries."""
    deadline = time.monotonic() + timeout
    windows: list[dict] = []
    while time.monotonic() < deadline:
        windows = terminal_windows(app)
        if len(windows) >= count:
            return windows
        time.sleep(1)
    raise AssertionError(f"own windows never reached {count}: {windows}")


def wait_container_ready(app, timeout: float = 90) -> None:
    """The client's containerReady flag — the spectator gate's honest
    precondition (no own windows is only meaningful once the container
    is up and the terminal_start round-trip has had its chance)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            app.terminal_eval("return st!.widget.wsClient.containerReady.toString();")
            == "true"
        ):
            return
        time.sleep(1)
    raise AssertionError("container never became ready")


def wait_shared_entry(app, window_id: str, present: bool, timeout: float = 30):
    """Poll the shared list until ``window_id`` is (not) in it."""
    deadline = time.monotonic() + timeout
    entries: list[dict] = []
    while time.monotonic() < deadline:
        entries = [s for s in shared_terminals(app) if s["window_id"] == window_id]
        if bool(entries) == present:
            return entries
        time.sleep(1)
    raise AssertionError(f"shared entry {window_id!r} never became present={present}")


def buffer_until(app, predicate, timeout: float = 60) -> str:
    """Poll the buffer until ``predicate`` holds; returns the passing
    buffer (the retry carrier — commands race pty bring-up). An
    unmounted terminal (another pane active) is remounted via the
    Terminal tab."""
    deadline = time.monotonic() + timeout
    buffer = ""
    while time.monotonic() < deadline:
        buffer = app.terminal_buffer()
        if predicate(buffer):
            return buffer
        if "NO-TERMINAL-STATE" in buffer:
            app.tap_labeled_exact("Terminal")
        time.sleep(2)
    raise AssertionError(f"buffer never satisfied the predicate: {buffer!r}")


def run_output(app, command: str, pattern: str, timeout: float = 60) -> str:
    """Send a command and return the buffer once its OUTPUT matches
    ``pattern`` (multiline). The marker must anchor to output lines —
    a bare substring matches the echoed input and returns before the
    command has run."""
    app.terminal_send(f"{command}\n")
    return buffer_until(app, lambda b: re.search(pattern, b, re.M) is not None, timeout)


def assert_terminal_focused(app) -> None:
    """The issue's Done-when: the focus tree names the terminal as the
    primary focus before any typing."""
    dump = str(app.exec("debug_dump_focus_tree", {}))
    assert "ghostty-terminal" in dump, dump[:400]


# --- scenarios ---------------------------------------------------------


def test_gate_own_terminals_by_role(harness, app):
    # code-in-isolation holders: own windows, the '+' strip, and focus
    for email in (ADMIN_EMAIL, CODER_EMAIL, COLLAB_EMAIL):
        at_login(harness, app)
        expect = "fmtk-verify" if email == ADMIN_EMAIL else "No workspaces yet"
        app.login(email, FIXTURE_PASSWORD, expect_text=expect)
        open_fixture_workspace(app, email)
        wait_terminal_windows(app, 1)
        assert find_label_nodes(app.snapshot(), "New terminal"), email
        assert_terminal_focused(app)
        app.logout()
    # the spectator: the Terminal tab mounts (it hosts shared terminals)
    # but the own-terminal UI does not — no windows, no '+', typing
    # inert (no pty behind the view: the echo never renders)
    at_login(harness, app)
    app.login(SPECTATOR_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
    open_fixture_workspace(app, SPECTATOR_EMAIL)
    wait_container_ready(app)
    time.sleep(3)  # the terminal_start round-trip had its chance
    assert terminal_windows(app) == []
    assert not find_label_nodes(app.snapshot(), "New terminal")
    app.terminal_send(f"echo GATE-INERT-{RUN}\n")
    time.sleep(3)
    assert f"GATE-INERT-{RUN}" not in app.terminal_buffer()
    app.logout()


def test_command_round_trip(harness, app):
    at_login(harness, app)
    register_user(harness, app, TERM_EMAIL, TERM_PW)
    create_workspace(app, TABS_WS)
    open_scratch_workspace(app, TABS_WS)
    wait_terminal_windows(app, 1)
    assert_terminal_focused(app)
    # echo + the acting user's identity in the same round-trip
    buffer = run_output(app, "echo hi; echo WHOAMI=$(whoami)", r"^WHOAMI=\S+$")
    assert re.search(r"^hi$", buffer, re.M), buffer
    who = re.search(r"^WHOAMI=(\S+)$", buffer, re.M)
    assert who and who.group(1), buffer
    # exit codes surface
    buffer = run_output(app, "sh -c 'exit 7'; echo RC:$?", r"^RC:7$")
    assert buffer
    # long output renders into the scrollback — the visible rows carry
    # the tail (row 1 scrolled past, 200 and the marker remain)
    buffer = run_output(app, "seq 1 200; echo SEQDONE", r"^SEQDONE$")
    assert re.search(r"^199$", buffer, re.M), buffer
    assert re.search(r"^200$", buffer, re.M), buffer


def test_own_terminal_tabs(harness, app):
    app.navigate("/workspaces")
    app.wait_for_text(TABS_WS)
    app.tap_button_exact(TABS_WS)
    app.wait_for_text("Terminal", 60000)
    app.tap_labeled_exact("Terminal")
    # '+' creates a second window (the UI path); unnamed windows all
    # default to 'term', so give the first a distinct name through the
    # client method the rename dialog invokes (the dialog itself opens
    # from a right-click the driver cannot perform)
    first = wait_terminal_windows(app, 1)[0]
    app.terminal_eval(
        f"st!.widget.wsClient.sendTerminalRenameWindow({first['index']}, 'main');"
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if any(w["name"] == "main" for w in terminal_windows(app)):
            break
        time.sleep(1)
    else:
        raise AssertionError("rename never landed")
    app.tap_labeled_exact("New terminal")
    windows = wait_terminal_windows(app, 2)
    second = next(w for w in windows if w["id"] != first["id"])
    # the view keeps the first window selected on '+' (#2176): typing
    # still lands there, not in the brand-new window
    run_output(app, f"echo TAB-M1-{RUN}", rf"^TAB-M1-{RUN}$")
    # switch to the new tab by its surfaced name and type there
    app.tap_labeled_exact(second["name"])
    run_output(app, f"echo TAB-M2-{RUN}", rf"^TAB-M2-{RUN}$")
    # switch back: the first window's buffer survived (tmux redraws it)
    app.tap_labeled_exact("main")
    buffer_until(app, lambda b: f"TAB-M1-{RUN}" in b)
    # close the second window from its tab's close icon; the client
    # list drops to one window and its tab leaves the strip
    app.tap_labeled_exact(second["name"])
    app.tap_labeled_exact(f"Close {second['name']}")
    wait_terminal_windows(app, 1)
    assert terminal_windows(app)[0]["name"] == "main"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not find_label_nodes(app.snapshot(), second["name"], exact=True):
            break
        time.sleep(1)
    else:
        raise AssertionError("closed tab never left the strip")
    # the terminal scenarios on this workspace are done; clean it up
    delete_from_list(app, TABS_WS)
    app.logout()


class SharedTerminalProducer(threading.Thread):
    """A second, real WS session holding a shared terminal open while
    the UI-driven spectator watches.

    A share does not outlive its producer's connection, so the
    collaborator cannot simply log out and let the spectator in — the
    producer stays connected on a background thread (the
    dual-connection shape the Playwright shared-terminal specs use),
    sharing one window and typing the marker, until :meth:`stop`
    unshares and disconnects.
    """

    def __init__(self, base_url: str, email: str, password: str, marker: str):
        super().__init__(daemon=True)
        self.base_url = base_url
        self.email = email
        self.password = password
        self.marker = marker
        self.ready = threading.Event()
        self.failed: Exception | None = None
        self._stop = threading.Event()
        self.window_id = ""

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=30)

    def run(self) -> None:
        try:
            asyncio.run(self._drive())
        except Exception as exc:  # surfaced via .failed in the test
            self.failed = exc
            self.ready.set()

    async def _drive(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"identifier": self.email, "password": self.password},
            ) as resp:
                token = (await resp.json())["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(
                f"{self.base_url}/api/v1/workspaces/shared", headers=headers
            ) as resp:
                wid = next(
                    w["id"] for w in await resp.json() if w["name"] == "fmtk-verify"
                )
            async with session.ws_connect(
                f"{self.base_url.replace('http', 'ws')}/ws",
                protocols=("bearer", token),
            ) as ws:
                await self._handshake(ws, wid)
                await ws.send_json(
                    {"cmd": "unshare_window", "window_id": self.window_id}
                )

    async def _handshake(self, ws, wid: str) -> None:
        await ws.send_json({"cmd": "workspace_connect", "workspaceId": wid})
        windows: list[dict] = []
        shared = False
        while not (windows and shared):
            frame = (await asyncio.wait_for(ws.receive(), 60)).json()
            kind = frame.get("type")
            if kind == "container_ready":
                await ws.send_json({"cmd": "ui_ready"})
            elif (
                kind == "event"
                and frame.get("event", {}).get("name") == "container_ready"
            ):
                await ws.send_json({"cmd": "terminal_start", "cols": 120, "rows": 30})
            elif kind == "error":
                raise RuntimeError(f"producer error frame: {frame}")
            elif kind == "terminal_windows":
                windows = frame["windows"]
            elif kind == "shared_terminals" and self.window_id:
                shared = any(
                    s.get("window_id") == self.window_id
                    for s in frame.get("terminals", [])
                )
            if windows and not self.window_id:
                self.window_id = windows[0]["id"]
                await ws.send_json({"cmd": "share_window", "window_id": self.window_id})
        # type the marker through the real input path; the pty executes
        await ws.send_json({"cmd": "terminal_input", "data": f"echo {self.marker}\n"})
        self.ready.set()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(ws.receive(), 0.5)
            except asyncio.TimeoutError:
                pass


def test_shared_terminal_spectator_watches(harness, app):
    marker = f"SHARED-M-{RUN}"
    producer = SharedTerminalProducer(
        harness.backend.url, COLLAB_EMAIL, FIXTURE_PASSWORD, marker
    )
    producer.start()
    if not producer.ready.wait(90):
        raise AssertionError(f"producer never shared: {producer.failed!r}")
    assert producer.failed is None, producer.failed
    try:
        # the spectator: the Terminal tab hosts the shared terminal as a
        # distinct tab; joining renders the producer's output read-only
        at_login(harness, app)
        app.login(SPECTATOR_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
        open_fixture_workspace(app, SPECTATOR_EMAIL)
        entries = wait_shared_entry(app, producer.window_id, present=True)
        app.tap_labeled_exact(
            f"Shared:{entries[0]['handle']}:{entries[0]['window_name']}"
        )
        buffer_until(app, lambda b: marker in b, 60)
        app.logout()
    finally:
        producer.stop()
        assert producer.failed is None, producer.failed


def test_sudo_ceiling_swap_needs_no_relogin(harness, app):
    # deterministic baseline: the deploy ceiling off (the stock posture)
    harness.backend.swap_settings({"allow_sudo": "false"}, apply="sighup", verify=False)
    # the workspace opts into sudo in its bag; the bag alone grants
    # nothing while the ceiling is off (#3047: bag AND deploy)
    token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
    status, body = http_api(
        harness.backend.url,
        token,
        "POST",
        "/api/v1/workspaces",
        {"name": SUDO_WS, "settings": {"allow_sudo": True}},
    )
    assert status in (200, 201), body
    try:
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
        open_scratch_workspace(app, SUDO_WS)
        wait_terminal_windows(app, 1)
        buffer = run_output(app, "sudo -n true; echo EXIT:$?", r"^EXIT:1$")
        assert buffer
        # flip the ceiling over SIGHUP — the reload drains workspaces,
        # stopping this container — then open the workspace again (the
        # connect auto-start): the boot path rewrites the sudoers rule
        # from the bag AND the new ceiling. Same session, no re-login.
        harness.backend.swap_settings(
            {"allow_sudo": "true"}, apply="sighup", verify=False
        )
        time.sleep(5)  # let the drain settle
        app.navigate("/workspaces")
        app.wait_for_text(SUDO_WS)
        open_scratch_workspace(app, SUDO_WS)
        wait_terminal_windows(app, 1)
        buffer = run_output(app, "sudo -n true; echo EXIT:$?", r"^EXIT:0$")
        assert buffer
        delete_from_list(app, SUDO_WS)
        app.logout()
    finally:
        harness.backend.swap_settings(
            {"allow_sudo": "false"}, apply="sighup", verify=False
        )
