"""fmtk e2e: the Network tab — interactive-egress consent rules and
banner state (#3237).

The consent surface is the workspace page's third gated tab: it mounts
only when the workspace is in ``egress_mode='interactive'`` AND the
member holds ``egress-consent`` (owners wildcard it; spectators never
do — #2883). Its panel (``consent_rules_panel.dart``) lists the static
allow/reject lists and the active verdicts with revoke + pause
controls; its banner (``consent_banner.dart``) surfaces held egress
requests with per-row Allow/Deny split buttons.

Every egress attempt is driven from the real terminal — a backgrounded
``curl`` whose exit code is the enforcement outcome, read from the
buffer (the #3235 discipline: markers anchor to output lines, never the
echoed input):

- deny → the sidecar forges an RST → ``curl`` exits 7 immediately;
- allow → the held SYN is released → ``curl`` exits 0 with real output;
- pause → prompts are silenced: no banner, off-list egress auto-allows.

Coverage: the tab gate (spectator sees no Network tab on the same
interactive workspace the owner does; a static workspace shows none for
its owner either, and flipping the mode over the workspace settings
API + a container restart mounts it), the full request→banner→verdict
→terminal-outcome loop in both deny and allow paths with the resulting
rule rows revoked afterwards, a ``Forever`` verdict picked from the
banner's duration menu (it persists into the workspace's static
allow-list and retracts on revoke — the next request prompts again),
and the pause window (auto-allow while paused, prompts return on
unpause).

Scenarios share sessions in definition order: 1 mixes fixture logins
with an admin-owned scratch workspace it deletes; 2 is a self-contained
fixture login that leaves no rules behind; 3-4 drive one run-registered
user's scratch workspace (4 deletes it). Re-run the whole file
(``-k`` selections break the chain — names and users are run-unique).

The fixture workspace ``fmtk-verify`` is interactive by default; its
only mutations are tilrestart verdicts that are revoked in the same
scenario, so it is left as found.
"""

from __future__ import annotations

import re
import time
import uuid

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    find_label_nodes,
    find_nodes,
    http_api,
    http_login,
    node_labels,
    node_type,
    wait_for_fields,
)

RUN = uuid.uuid4().hex[:6]
NET_EMAIL = f"fmtk-net{RUN}@example.com"
NET_PW = f"fmtk-Net{RUN}!C6"
MODE_WS = f"fmtk-mode-{RUN}"
RULES_WS = f"fmtk-rules-{RUN}"
SPECTATOR_EMAIL = "fmtk-spectator@example.com"

# Fresh destinations per scenario: a verdict for one host never covers
# another, and re-running against a kept backend must not inherit the
# previous run's rules.
DENY_HOST = "example.com"
ALLOW_HOST = "example.org"
PAUSE_HOST = "example.net"


# --- shared driving helpers (suite-local; harness keeps primitives) ----


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
    """Register + email-verify; the auto-login lands on the empty list.
    The fields are grabbed through the page-marker wait (#3264): a
    snapshot between the tap and the route swap returns the login
    page's fields and the typed text dies with them."""
    app.tap_label("Need an account? Create one")
    fields = wait_for_fields(app, "Create Account")
    app.enter_text(fields[0]["ref"], email)
    app.enter_text(fields[1]["ref"], password)
    app.tap_label("Create Account")
    app.wait_for_text("Check your email to verify your account.")
    token = harness.smtp.token_for("verify", email)
    app.navigate(f"/verify?token={token}")
    app.wait_for_text("No workspaces yet. Create one to get started.")


def open_scratch_workspace(app, name: str) -> None:
    """Open a scratch workspace tile robustly. The route bounce first
    (``/settings`` → ``/workspaces``) is not optional: the API-created
    workspace is invisible to a list page that is already mounted (a
    same-route navigate does not remount it, and the workspaces-changed
    push does not reach a page created before the workspace existed),
    so the return trip must remount the list to fetch the new tile.
    Then scroll the tile into the viewport, tap it as a button, and
    retry the open — a tap can land while the list is still settling
    after a login."""
    for _ in range(3):
        app.navigate("/settings")
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


def open_fixture_workspace(app, email: str) -> None:
    """Open ``fmtk-verify`` for a fixture member: the owner finds it
    under Owned by Me, role members tap the Shared with Me segment
    first. The workspace page's Terminal tab is the mount signal."""
    app.navigate("/workspaces")
    if email != ADMIN_EMAIL:
        app.tap_labeled_exact("Shared with Me")
    app.wait_for_text("fmtk-verify")
    app.tap_button_exact("fmtk-verify")
    app.wait_for_text("Terminal", 60000)
    app.tap_labeled_exact("Terminal")


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


def assert_terminal_focused(app) -> None:
    """The focus tree names the terminal as the primary focus before
    any typing."""
    dump = str(app.exec("debug_dump_focus_tree", {}))
    assert "ghostty-terminal" in dump, dump[:400]


def host_nodes(app, host: str) -> list[dict]:
    """Snapshot nodes showing ``host`` as a destination. The match carries
    the trailing ``:`` (the banner/rule rows render ``host:port``): the
    app-bar email chip contains the bare host as a substring
    (``<user>@example.com``) and would match forever, so absence polls
    keyed on the host alone can never pass."""
    return find_label_nodes(app.snapshot(), f"{host}:")


def wait_banner(app, host: str, timeout: float = 60) -> None:
    """Wait until the consent banner shows a held request for ``host``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_label_nodes(app.snapshot(), "Pending egress consent"):
            if host_nodes(app, host):
                return
        time.sleep(1)
    raise AssertionError(f"no consent banner for {host}")


def assert_no_banner(app, seconds: float = 8) -> None:
    """The banner must stay absent for the whole window (a pause proves
    itself by NOT prompting)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert not find_label_nodes(app.snapshot(), "Pending egress consent")
        time.sleep(1)


EG_SEQ = 0


def held_curl(app, host: str) -> str:
    """Fire a backgrounded egress attempt at ``host``; the shell prompt
    returns immediately, the verdict's outcome lands in ``EG-<seq>:N``
    (deny → forged RST → 7; allow → released SYN → 0). Each call gets a
    fresh sequence tag — the buffer accumulates, so a shared marker
    would let a later assert read an earlier verdict's stale code."""
    global EG_SEQ
    EG_SEQ += 1
    tag = f"EG-{EG_SEQ}"
    app.terminal_eval("st!.requestFocus();")
    assert_terminal_focused(app)
    app.terminal_send(
        f"(curl -sS -m 90 -o /dev/null http://{host}/; echo {tag}:$?) &\n"
    )
    return tag


def wait_exit_code(app, tag: str, timeout: float = 90) -> int:
    """The tagged curl's exit code from the buffer. The echoed command
    carries ``<tag>:$?`` (no digit), so the marker can only match the
    real output — which does NOT start at column 0: a successful
    backgrounded curl reports at the next prompt (``~$ EG-1:0``), so a
    line-start anchor would miss exactly the allow path."""
    pattern = rf"{tag}:(\d+)"
    buffer = buffer_until(app, lambda b: re.search(pattern, b) is not None, timeout)
    return int(re.search(pattern, buffer).group(1))


def held_until_banner(app, host: str) -> str:
    """Fire curls at ``host`` until one is actually held (banner shows
    it). A curl whose DNS lookup fails transiently exits 6 with no SYN
    — no hold, no banner — so an attempt that produces no banner within
    25s is assumed dead and re-fired (each with its own exit tag; the
    dead attempts' tags never resolve and are ignored)."""
    for _ in range(3):
        tag = held_curl(app, host)
        try:
            wait_banner(app, host, timeout=25)
            return tag
        except AssertionError:
            continue
    raise AssertionError(f"{host} was never held (three attempts)")


def _dump_sidecar_diag(host: str) -> None:
    """TEMP #3237 diagnostics: the sidecar's RST-debug lines + rules."""
    import subprocess

    ps = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}", "-q"],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.split()
    for name in ps:
        if "klangk-net" in name:
            logs = subprocess.run(
                ["podman", "logs", "--tail", "80", name],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
            print(f"SIDECAR-LOGS {name}:", logs[-4000:])
            rules = subprocess.run(
                ["podman", "exec", name, "iptables-save"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
            print(f"SIDECAR-RULES {name}:", rules[-3000:])


def verdict(app, decision: str, host: str) -> int:
    """The full loop for one host: hold, decide from the banner, and
    return the curl exit code the terminal sees."""
    tag = held_until_banner(app, host)
    app.tap_labeled_exact(decision)
    # the banner row must leave on the verdict (egress_resolved): if it
    # stays, the tap never landed -- retry once
    for _ in range(2):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not find_label_nodes(app.snapshot(), "Pending egress consent"):
                break
            time.sleep(1)
        else:
            app.tap_labeled_exact(decision)
            continue
        break
    try:
        return wait_exit_code(app, tag)
    except AssertionError:
        _dump_sidecar_diag(host)
        raise


def choose_duration(app, host: str, item: str) -> None:
    """Open the Allow row's duration menu and pick ``item``. The ``▾``
    segments carry only tooltips (tooltips do not reach the semantic
    tree), so the segment is addressed by reading order: within one
    banner row the buttons run Allow, Allow's ``▾``, Deny, Deny's
    ``▾`` — the second button after the row's text is Allow's
    segment. One pending request at a time keeps the order stable."""
    nodes = find_nodes(app.snapshot(), lambda n: True)
    idx = next(
        (i for i, n in enumerate(nodes) if f"{host}:" in " ".join(node_labels(n))),
        None,
    )
    assert idx is not None, f"no banner row for {host}"
    buttons = [n for n in nodes[idx + 1 :] if node_type(n) == "button" and n.get("ref")]
    assert len(buttons) >= 2 and "Allow" in " ".join(node_labels(buttons[0])), (
        f"unexpected banner row buttons: {buttons[:2]}"
    )
    app.tap(buttons[1]["ref"])
    app.wait_for_text(item)
    app.tap_labeled_exact(item)


def open_network_tab(app) -> None:
    app.tap_labeled_exact("Network")
    app.wait_for_text("Egress consent rules")


def revoke_rule(app, host: str) -> None:
    """Revoke the active rule for ``host`` from the panel. The row's
    revoke affordance is an icon button with a tooltip only (tooltips
    do not reach the semantic tree), so it is addressed as the first
    button after the row's text in reading order; the confirm dialog
    is part of the flow."""
    nodes = find_nodes(app.snapshot(), lambda n: True)
    idx = next(
        (i for i, n in enumerate(nodes) if f"{host}:" in " ".join(node_labels(n))),
        None,
    )
    assert idx is not None, f"no rule row for {host}"
    btn = next(
        (n for n in nodes[idx + 1 :] if node_type(n) == "button" and n.get("ref")),
        None,
    )
    assert btn is not None, f"no revoke button on the {host} row"
    for _ in range(3):
        app.tap(btn["ref"])
        app.wait_for_text("Revoke consent rule?")
        app.tap_button_exact("Revoke")
        try:
            app.wait_gone("Revoke consent rule?", 10000)
            break
        except FmtkError:
            continue
    else:
        raise AssertionError("revoke dialog never closed")
    # the row leaves only on the server's revoke_ack — poll for it
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not host_nodes(app, host):
            return
        time.sleep(1)
    raise AssertionError(f"rule row for {host} never left the panel")


def workspace_id(harness, name: str) -> str:
    """The scratch workspace's id (looked up as its owner)."""
    token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
    status, listing = http_api(harness.backend.url, token, "GET", "/api/v1/workspaces")
    for ws in listing:
        if ws["name"] == name:
            return ws["id"]
    raise AssertionError(f"workspace {name} not found")


# --- scenarios ---------------------------------------------------------


def test_network_tab_gated_by_mode_and_permission(harness, app):
    # the spectator holds join/terminal/spectate but not egress-consent:
    # the same interactive workspace that shows its owner the Network
    # tab shows the spectator none
    at_login(harness, app)
    app.login(SPECTATOR_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
    open_fixture_workspace(app, SPECTATOR_EMAIL)
    assert not find_label_nodes(app.snapshot(), "Network", exact=True), (
        "spectator sees a Network tab"
    )
    app.logout()

    # the owner (wildcard permissions) sees the tab and the live panel
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    open_fixture_workspace(app, ADMIN_EMAIL)
    open_network_tab(app)
    app.wait_for_text("Static allow-list")
    app.wait_for_text("Pause prompts")

    # a STATIC workspace shows its owner no Network tab even with the
    # same wildcard permissions — the tab is mode-driven, not role-driven
    token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
    status, _ = http_api(
        harness.backend.url,
        token,
        "POST",
        "/api/v1/workspaces",
        {"name": MODE_WS, "egress_mode": "static"},
    )
    assert status == 200, status
    open_scratch_workspace(app, MODE_WS)
    assert not find_label_nodes(app.snapshot(), "Network", exact=True), (
        "static workspace shows a Network tab"
    )

    # flip the mode over the settings API and restart the container
    # (egress_mode is enforced by the network sidecar at container
    # create) — re-entering the page mounts the tab
    ws_id = workspace_id(harness, MODE_WS)
    status, _ = http_api(
        harness.backend.url,
        token,
        "PUT",
        f"/api/v1/workspaces/{ws_id}",
        {"egress_mode": "interactive"},
    )
    assert status == 200, status
    http_api(harness.backend.url, token, "POST", f"/api/v1/workspaces/{ws_id}/stop")
    time.sleep(3)
    open_scratch_workspace(app, MODE_WS)
    open_network_tab(app)
    app.wait_for_text("Static allow-list")

    delete_from_list(app, MODE_WS)
    app.logout()


def test_request_banner_deny_and_allow_loop(harness, app):
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    open_fixture_workspace(app, ADMIN_EMAIL)

    # deny: the held SYN is answered with a forged RST — curl exits 7
    # and the verdict lands as an active deny rule
    assert verdict(app, "Deny", DENY_HOST) == 7
    open_network_tab(app)
    app.wait_for_text("Active denies (1)")
    revoke_rule(app, DENY_HOST)
    app.wait_for_text("Active denies (0)")

    # allow: the SYN is released — curl exits 0 with the page fetched
    app.tap_labeled_exact("Terminal")
    assert verdict(app, "Allow", ALLOW_HOST) == 0
    open_network_tab(app)
    app.wait_for_text("Active allows (1)")
    revoke_rule(app, ALLOW_HOST)
    app.wait_for_text("Active allows (0)")
    app.logout()


def test_forever_verdict_persists_and_revokes(harness, app):
    at_login(harness, app)
    register_user(harness, app, NET_EMAIL, NET_PW)
    token = http_login(harness.backend.url, NET_EMAIL, NET_PW)
    status, _ = http_api(
        harness.backend.url,
        token,
        "POST",
        "/api/v1/workspaces",
        {"name": RULES_WS},
    )
    assert status == 200, status
    open_scratch_workspace(app, RULES_WS)

    # a Forever verdict from the banner's duration menu: the connection
    # is released AND the decision persists into the workspace's static
    # allow-list (a durable rule, not a tilrestart one)
    tag = held_until_banner(app, DENY_HOST)
    choose_duration(app, DENY_HOST, "Forever")
    assert wait_exit_code(app, tag) == 0
    open_network_tab(app)
    app.wait_for_text("Static allow-list")
    assert host_nodes(app, DENY_HOST), (
        "forever allow never reached the static allow-list"
    )

    # revoking the durable rule retracts the allow-list entry — the
    # next request prompts again instead of sailing through
    revoke_rule(app, DENY_HOST)
    assert not host_nodes(app, DENY_HOST), "allow-list entry survived the revoke"
    app.tap_labeled_exact("Terminal")
    tag = held_until_banner(app, DENY_HOST)
    app.tap_labeled_exact("Deny")
    assert wait_exit_code(app, tag) == 7
    open_network_tab(app)
    revoke_rule(app, DENY_HOST)


def test_pause_silences_prompts(harness, app):
    # same session as the forever scenario: RULES_WS is still open
    open_network_tab(app)
    app.wait_for_text("Pause prompts")
    app.tap_labeled_exact("Pause 15m")
    app.wait_for_text("Filtering paused")

    # while paused, an off-list egress auto-allows: no banner, and the
    # curl completes on its own
    app.tap_labeled_exact("Terminal")
    tag = held_curl(app, PAUSE_HOST)
    assert_no_banner(app)
    assert wait_exit_code(app, tag) == 0

    # unpause: prompts return (a fresh host is held again)
    open_network_tab(app)
    app.tap_labeled_exact("Unpause")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not find_label_nodes(app.snapshot(), "Filtering paused"):
            break
        time.sleep(1)
    else:
        raise AssertionError("pause status never cleared")

    delete_from_list(app, RULES_WS)
    app.logout()
