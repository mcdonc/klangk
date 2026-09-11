"""fmtk e2e: the git-credential OAuth authorization-code flow against the
real Gitea instance (#3385 phase 4).

The whole chain runs on real surfaces: a private repo clone in the
workspace terminal invokes ``git-credential-klangk`` (baked into the
image), which relays ``auth_flow_start`` over the browser bridge; the
feature's authorization dialog appears in the app tab; the popup is
opened from the app tab with ``window.open`` at the pending authorize
URL read from the feature state (the driven Chrome runs with popup
blocking disabled — the dialog's "Reopen" link is a RichText span,
not a semantic tappable, and a synthetic CDP pointer click at its
bounds corrupts the widget build scope); Gitea's login + authorize
forms are driven in the popup tab over CDP; Gitea redirects the popup
to the proxy origin root with ``?code&state``, the SPA boots to
``/git-auth-callback`` and posts the code to the opener; the helper
exchanges the code CONTAINER-side (``host.containers.internal`` — the
same host-gateway pattern the bridge URL itself uses) and git finishes
the clone.

The popup's app boot breaks the flutter tool's dwds eval pipe to the
app tab for the rest of the scenario (every fmtk exec fails with no
JSON envelope), so the popup legs drive over CDP only and the
completion asserts read host-side podman-exec sentinels;
``app.restore_eval_pipe()`` re-attaches the pipe deterministically
before the post-test machinery (the conftest app-errors drain, the
next scenario) needs it (#3402).

The cancel leg runs the same relay up to the dialog and cancels from
the app side: git fails with terminal prompts disabled, proving the
held bridge request unwinds.

Scoping: Gitea is the shared ``gitea-e2e`` instance (reused when a
human already has it up); the OAuth application's redirect is the
proxy origin root, exactly what the phase-2 contract ships. The
provider entry rides ``features_config:`` in the scratch klangkd.yaml
(SIGHUP; the container env bridge reads it at workspace create) and is
restored in teardown. The scratch workspace is created via the API
with ``egress_mode='allow'`` (permit-with-deny-list — the container
must reach the host-gateway token endpoint without consent prompts,
which would need a second banner leg) and deleted by the same fixture.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid

import pytest

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    Gitea,
    cdp_close_tab,
    cdp_eval,
    cdp_eval_tab,
    cdp_tabs,
    http_api,
    http_login,
    write_config_yaml,
)

RUN = uuid.uuid4().hex[:6]
WS_NAME = f"fmtk-gitauth-{RUN}"
CLONE_DIR = "/tmp/gitauth-priv"
CLONE_OK = f"CLONE-OK-{RUN}"
REMOTE_OK = f"REMOTE-OK-{RUN}"
CANCEL_OK = f"CANCEL-OK-{RUN}"
GIT_HOST = f"host.containers.internal:{Gitea.PORT}"
CLONE_URL = f"http://{GIT_HOST}/gitea-admin/{Gitea.PRIVATE_REPO}.git"

# --- suite-local driving helpers (harness keeps primitives) ------------


def open_scratch_workspace(app, name: str) -> None:
    """Open a scratch workspace tile robustly (the #3235 pattern): the
    Terminal tab is the mount signal, and a tap can land while the list
    is still settling."""
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


def wait_container_ready(app, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (
                app.terminal_eval(
                    "return st!.widget.wsClient.containerReady.toString();"
                )
                == "true"
            ):
                return
        except FmtkError:
            pass  # dwds re-attach race — retry
        time.sleep(1)
    raise AssertionError("container never became ready")


def wait_terminal_windows(app, count: int, timeout: float = 90) -> list[dict]:
    """Poll the client's own-window list until it reaches ``count`` —
    the honest precondition for typing (an unwired pane swallows
    sendText silently, AGENTS.md)."""
    deadline = time.monotonic() + timeout
    windows: list[dict] = []
    while time.monotonic() < deadline:
        windows = json.loads(
            app.terminal_eval("return jsonEncode(st!.widget.wsClient.terminalWindows);")
        )
        if len(windows) >= count:
            return windows
        time.sleep(1)
    raise AssertionError(f"own windows never reached {count}: {windows}")


def terminal_eval_last(app, body: str) -> str:
    """Evaluate over the NEWEST terminal state. The harness's default
    walk stops at the first mounted state — a dead window 0 shadows the
    live successor the "+" button just created."""
    expression = (
        "() { List<GhosttyTerminalState> all = [];"
        " void walk(Element el) {"
        " if (el is StatefulElement && el.state is GhosttyTerminalState)"
        " all.add(el.state as GhosttyTerminalState);"
        " el.visitChildElements((c) => walk(c)); }"
        " walk(WidgetsBinding.instance.rootElement!);"
        " if (all.isEmpty) return 'NO-TERMINAL-STATE';"
        " var st = all.last; " + body + " }()"
    )
    result = app.exec(
        "evaluate_dart_expression",
        {
            "expression": expression,
            "libraryUri": "package:klangk_frontend/terminal/ghostty_terminal.dart",
        },
    )
    if isinstance(result, dict) and "result" in result:
        return str(result["result"])
    return str(result)


def buffer_last(app) -> str:
    return terminal_eval_last(
        app,
        "var f = st!._terminal.createFormatter("
        "format: FormatterFormat.plain, unwrap: true, trim: true); "
        "try { return f.format(); } finally { f.dispose(); }",
    )


def send_last(app, text: str) -> None:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("$", "\\$")
        .replace("'", "\\'")
        .replace("\n", "\\n")
    )
    terminal_eval_last(app, f"st!._terminal.sendText('{escaped}');")


def live_terminal(app, attempts: int = 3) -> None:
    """A prompted pane, proven by round-trip.

    The prompt is the honest liveness signal (the status line rendered
    above the grid carries an "Exit: Enter, then ~." hint on EVERY
    pane, healthy or not — it is not a death marker). A pane with no
    prompt yet (the container-start race on a fresh workspace) gets a
    fresh window via the "+" button — never more than a few, the tab
    strip overflows."""
    for _ in range(attempts):
        try:
            wait_terminal_windows(app, 1)
            run_output(app, "echo PROMPT-$?-OK", r"^PROMPT-0-OK$", timeout=30)
            return
        except (AssertionError, FmtkError):
            app.tap_labeled_exact("New terminal")
            time.sleep(3)
    raise AssertionError(
        f"the terminal never answered: {buffer_last(app)!r}; "
        f"tmux sessions: {_container_tmux_ls(app)}"
    )


def _container_tmux_ls(app) -> str:
    """Diagnostic for a dead pane: the container's tmux session table
    (the base session the grouped new-session targets)."""
    import subprocess

    out = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    for name in out:
        if "fmtk-gitauth" in name and not name.startswith("klangk-net-"):
            return subprocess.run(
                ["podman", "exec", "-u", "klangk", name, "tmux", "ls"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
    return "(no container found)"


def at_login(harness, app) -> None:
    """Land on the usable login form, ending any session an earlier
    scenario or run left live (a leftover token guards /login away —
    the router bounces a normal session to /workspaces)."""
    app.ensure_tab_at_app("/#/login")
    if not app.has_text("Log In", 5000):
        app.logout()
    app.wait_for_login_page()
    app.dismiss_login_banner()
    app.wait_for_text("Email or handle")


def login_admin(harness, app) -> None:
    """Login with retries: the post-login workspace list can lag a
    fresh app boot (storage-wipe grace + first paint) past the harness
    login wait's default; the retry rides it out instead of failing the
    scenario (the failed wait leaves a logged-in app — at_login resets
    it)."""
    last: Exception | None = None
    for _ in range(3):
        try:
            at_login(harness, app)
            # "Owned by Me" is the list surface itself — a fixed expect
            # tile ("fmtk-verify") falls off the first page once stale
            # scratch workspaces accumulate from aborted runs
            app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="Owned by Me")
            return
        except FmtkError as exc:
            last = exc
    raise AssertionError(f"admin login never settled: {last}")


def buffer_until(app, predicate, timeout: float = 120) -> str:
    """Poll the NEWEST pane's buffer until ``predicate`` holds (commands
    race pty bring-up; an unmounted terminal is remounted via the
    Terminal tab)."""
    deadline = time.monotonic() + timeout
    buffer = ""
    while time.monotonic() < deadline:
        try:
            buffer = buffer_last(app)
        except FmtkError:
            pass  # dwds re-attach races the popup's page churn — retry
        if predicate(buffer):
            return buffer
        if "NO-TERMINAL-STATE" in buffer:
            try:
                app.tap_labeled_exact("Terminal")
            except FmtkError:
                pass  # the dwds race can hand back an empty snapshot
        time.sleep(2)
    tabs = []
    try:
        tabs = [t["url"][:80] for t in cdp_tabs()]
    except FmtkError:
        tabs = ["(chrome gone)"]
    raise AssertionError(
        f"buffer never satisfied the predicate: {buffer!r}; tabs: {tabs}"
    )


def run_output(app, command: str, pattern: str, timeout: float = 120) -> str:
    """Send a command to the NEWEST pane and return the buffer once its
    OUTPUT matches ``pattern`` (multiline — markers must anchor to
    output lines, never the echoed input)."""
    send_last(app, f"{command}\n")
    return buffer_until(app, lambda b: re.search(pattern, b, re.M) is not None, timeout)


DONE_FILE = "/tmp/gitauth-clone-done"


def _workspace_container(gitea) -> str:
    """The running workspace container (the suite's fmtk-gitauth one)."""
    out = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split()
    for name in out:
        if "fmtk-gitauth" in name and not name.startswith("klangk-net-"):
            return name
    raise AssertionError(f"no running gitauth container: {out}")


def _podman_exec(gitea, argv: list[str]) -> list[str]:
    return ["podman", "exec", "-u", "klangk", _workspace_container(gitea), *argv]


def wait_clone_sentinel(gitea, timeout: float = 300) -> None:
    """Block until the pane's clone finished (sentinel file in the
    container)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = subprocess.run(
            _podman_exec(gitea, ["test", "-f", DONE_FILE]),
            capture_output=True,
            timeout=30,
        )
        if probe.returncode == 0:
            return
        time.sleep(3)
    raise AssertionError("the clone never completed (no sentinel file)")


def trace_tabs(label: str) -> None:
    try:
        from fmtkharness import cdp_tabs

        tabs = [t["url"][:70] for t in cdp_tabs()]
        print(f"[tabs:{label}] {tabs}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[tabs:{label}] failed: {exc}", flush=True)


def wait_gitea_popup(gitea: Gitea, timeout: float = 30) -> dict:
    """The popup tab the Reopen click opened — the one non-app page
    tab sitting on the Gitea origin."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tab in cdp_tabs():
            if tab["url"].startswith(gitea.base):
                return tab
        time.sleep(0.5)
    raise AssertionError(
        f"no popup on {gitea.base} (tabs: {[t['url'] for t in cdp_tabs()]})"
    )


def pending_authorize_url(app) -> str:
    """The held flow's authorize URL, read from the feature state.

    The Reopen link's RichText span is not a semantic tappable, and a
    synthetic CDP pointer click at its bounds corrupts the widget build
    scope (the app's red error screen) — so the suite reads the URL the
    helper sent and opens the popup itself via ``window.open`` (the
    driven browser runs with popup blocking disabled). Evaluated in the
    feature's own library, where the private overlay-state type is
    nameable."""
    expression = (
        "() { String? url;"
        " void walk(Element el) {"
        " if (url != null) return;"
        " final st = el is StatefulElement ? el.state : null;"
        " if (st is _CredentialOverlayState) {"
        " final p = st.widget.feature._pendingAuth;"
        " url = p?.authorizeUrl;"
        " return; }"
        " el.visitChildElements((c) => walk(c)); }"
        " walk(WidgetsBinding.instance.rootElement!);"
        " return url ?? 'NO-FLOW'; }()"
    )
    result = app.exec(
        "evaluate_dart_expression",
        {
            "expression": expression,
            "libraryUri": "package:klangk_feature_git_credential/feature.dart",
        },
    )
    if isinstance(result, dict) and "result" in result:
        result = result["result"]
    url = str(result)
    assert url.startswith(gitea_base()), f"no pending authorize url: {url!r}"
    return url


def gitea_base() -> str:
    from fmtkharness import Gitea

    return f"http://127.0.0.1:{Gitea.PORT}"


def open_authorize_popup(app, url: str) -> None:
    """Open the authorize popup from the app tab (its ``window.opener``
    is the app tab — exactly what the callback page posts back to)."""
    cdp_eval(f"window.open({json.dumps(url)}, '_blank')")


def gitea_login_and_authorize(gitea: Gitea, popup: dict) -> None:
    """Drive Gitea's login + authorize forms in the popup tab.

    An unauthenticated authorize hit lands on the login form (Gitea
    keeps the ``redirect_to`` chain); after login the authorize page's
    grant form is submitted, which redirects the popup to the klangk
    proxy origin root with ``?code&state``. Each leg retries — Gitea's
    autofocus can clear a just-filled field and a single missed click
    would strand the flow on a form.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        url = cdp_tabs_url(popup)
        if "/user/login" in url or "/login/oauth" in url:
            break
        time.sleep(0.5)
    else:
        raise AssertionError(
            f"popup never reached a Gitea form (at {cdp_tabs_url(popup)!r})"
        )

    login_js = """
      (function(){
        const user = document.querySelector(
          'input[name=user_name], input[name=user]');
        const pass = document.querySelector(
          'input[name=password], input[type=password]');
        if (!user || !pass) return 'no-fields';
        user.value = '%s'; pass.value = '%s';
        const form = user.closest('form');
        const btn = form && form.querySelector('button');
        if (btn) btn.click();
        if (form) form.submit();
        return 'submitted';
      })()
    """ % (Gitea.ADMIN_USER, Gitea.ADMIN_PASSWORD)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if "/user/login" not in cdp_tabs_url(popup):
            break  # login landed (or was never needed)
        try:
            state = cdp_eval_tab(popup, login_js)
        except FmtkError:
            state = "retry"
        time.sleep(2)
    else:
        raise AssertionError(
            f"the Gitea login form never submitted (at {cdp_tabs_url(popup)!r})"
        )

    # the authorize (grant) page after login — its form posts the grant
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            state = cdp_eval_tab(
                popup,
                """
                (function(){
                  const grant = document.querySelector(
                    'form[action*="grant"] button');
                  if (grant) { grant.click(); return 'granted'; }
                  return location.pathname;
                })()
                """,
            )
            if state == "granted":
                return
        except FmtkError:
            pass  # page mid-navigation — retry
        time.sleep(1)
    raise AssertionError("the Gitea authorize form never appeared in the popup")


def retire_popup_after_landing(popup: dict, timeout: float = 120) -> None:
    """Dispose of the popup once its chain lands on the callback route.

    CDP-only by design: fmtk evals during the popup's SPA boot churn the
    dwds isolate list ("No Flutter isolate" while the popup's app is
    mid-registration), which arms the harness's wedge recovery — its
    flutter restart kills the app tab and the held flow with it. The
    callback route in the popup's URL proves the code was handed to the
    app (the page posts in initState); a short grace covers the post,
    then the tab is closed so the second isolate goes away before any
    later eval."""
    from fmtkharness import PROXY_PORT

    origin = f"http://127.0.0.1:{PROXY_PORT}"
    deadline = time.monotonic() + timeout
    landed = False
    while time.monotonic() < deadline:
        url = cdp_tabs_url(popup)
        if not url:
            return  # it closed itself after posting
        if url.startswith(origin) and "/git-auth-callback" in url:
            landed = True
            break
        time.sleep(1)
    if landed:
        time.sleep(5)  # the callback page posts to the opener in initState
    cdp_close_tab(popup)


def cdp_tabs_url(tab: dict) -> str:
    """Re-read a tab's URL (it navigates during the chain)."""
    for candidate in cdp_tabs():
        if candidate["id"] == tab["id"]:
            return str(candidate["url"])
    return ""


def workspace_id(harness, name: str) -> str:
    token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
    _, listing = http_api(harness.backend.url, token, "GET", "/api/v1/workspaces")
    for ws in listing:
        if ws["name"] == name:
            return ws["id"]
    raise AssertionError(f"workspace {name} not found")


# --- fixtures ----------------------------------------------------------


@pytest.fixture(scope="module")
def gitea() -> Gitea:
    instance = Gitea()
    instance.ensure()
    return instance


@pytest.fixture(scope="module")
def provider_workspace(harness, gitea):
    """Swap the OAuth provider config into the scratch config (SIGHUP —
    the container env bridge reads it at workspace create), create the
    scratch workspace in permit-egress mode, and tear both down.

    This stays a provider-map entry rather than the
    KLANGKWS_FEATURE_GITEA_OAUTH_CLIENT_ID shorthand (#3405) on
    purpose: the shorthand derives the authorize and token URLs from
    the clone host, but this topology reaches Gitea by two names —
    127.0.0.1 for the driven Chrome's authorize popup,
    host.containers.internal for the container-side token exchange —
    exactly the split the map exists for (unit tests cover the
    shorthand's own expansion, precedence, and fallback)."""
    entry = {
        "host": GIT_HOST.rsplit(":", 1)[0],
        "flow": "authorization_code_pkce",
        "client_id": gitea.oauth_client()["client_id"],
        "authorize_url": f"{gitea.base}/login/oauth/authorize",
        "token_url": f"http://{GIT_HOST}/login/oauth/access_token",
        "redirect_uri": f"http://127.0.0.1:{_proxy_port()}/",
    }
    token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
    old = harness.config.get("features_config")
    harness.config["features_config"] = {
        "KLANGKWS_FEATURE_OAUTH_PROVIDERS": json.dumps([entry])
    }
    write_config_yaml(harness.config)
    harness.backend.sighup()
    harness.backend.wait_healthy()
    status, _ = http_api(
        harness.backend.url,
        token,
        "POST",
        "/api/v1/workspaces",
        {"name": WS_NAME, "egress_mode": "allow"},
    )
    assert status == 200, status
    try:
        yield {"name": WS_NAME, "id": workspace_id(harness, WS_NAME)}
    finally:
        try:
            http_api(
                harness.backend.url,
                token,
                "DELETE",
                f"/api/v1/workspaces/{workspace_id(harness, WS_NAME)}",
            )
        except AssertionError:
            pass  # a hard-killed earlier run may already have removed it
        if old is None:
            harness.config.pop("features_config", None)
        else:
            harness.config["features_config"] = old
        write_config_yaml(harness.config)
        harness.backend.sighup()
        harness.backend.wait_healthy()


def _proxy_port() -> str:
    from fmtkharness import PROXY_PORT

    return PROXY_PORT


# --- scenarios ---------------------------------------------------------


def test_browser_flow_clones_private_repo_and_reuses_cache(
    harness, app, gitea, provider_workspace
):
    login_admin(harness, app)
    open_scratch_workspace(app, WS_NAME)
    wait_container_ready(app)
    live_terminal(app)
    run_output(
        app,
        "printenv KLANGKWS_FEATURE_OAUTH_PROVIDERS >/dev/null && echo PROVIDERS-OK",
        r"^PROVIDERS-OK$",
    )

    # the clone demands credentials: the private repo 401s, git invokes
    # the helper, the helper relays auth_flow_start and the app tab
    # shows the dialog (displayHost = the authorize URL's host)
    # the clone lands in the newest pane; the marker anchors to its
    # output line
    send_last(
        app,
        # The marker file is the assertion surface: the dwds eval pipe
        # does not survive the popup's app boot, so the clone's
        # completion is proven from the host (podman exec), never from
        # the terminal buffer.
        f"GIT_TERMINAL_PROMPT=0 git clone {CLONE_URL} {CLONE_DIR} "
        f"&& echo {CLONE_OK} && touch {DONE_FILE}\n",
    )
    try:
        app.wait_for_text("Sign in to 127.0.0.1", 60000)
    except FmtkError as exc:
        raise AssertionError(
            f"the authorization dialog never appeared ({exc}); "
            f"terminal buffer:\n{buffer_last(app)}"
        ) from None
    app.wait_for_text("Waiting for authorization...")
    trace_tabs("dialog-up")

    # open the popup (http authorize URL is never auto-opened — the
    # https gate) and drive Gitea's login + grant forms in it; from
    # here on the eval pipe is at risk, so the flow runs under a
    # finally that restores it — even a failed leg must leave the
    # post-test machinery (app-errors drain, the next scenario) a
    # working pipe (#3402)
    authorize_url = pending_authorize_url(app)
    try:
        open_authorize_popup(app, authorize_url)
        trace_tabs("popup-opened")
        popup = wait_gitea_popup(gitea)
        gitea_login_and_authorize(gitea, popup)
        trace_tabs("granted")
        retire_popup_after_landing(popup)
        trace_tabs("retired")

        # the popup lands on the proxy origin, boots to the callback page,
        # delivers the code to the opener and closes itself; the helper
        # exchanges container-side and git finishes the clone — all without
        # the (now dead) eval pipe. The sentinel file proves the clone.
        wait_clone_sentinel(gitea)
        trace_tabs("clone-ok")

        # the cached credential serves the next credentialed operation with
        # no new browser flow: a fresh git from the host side shares the
        # pane's browser id (tmux global env), so its credential fill hits
        # the app tab's cache. A cache miss would hang the helper waiting
        # for a fresh authorization — the timeout turns that into rc!=0.
        probe = subprocess.run(
            _podman_exec(
                gitea,
                [
                    "bash",
                    "-lc",
                    f"timeout 30 git -C {CLONE_DIR} ls-remote origin"
                    " >/dev/null; echo rc=$?",
                ],
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert "rc=0" in probe.stdout, probe.stdout + probe.stderr
    finally:
        app.restore_eval_pipe()


def test_cancel_in_the_dialog_fails_the_clone(harness, app, provider_workspace):
    login_admin(harness, app)
    open_scratch_workspace(app, WS_NAME)
    wait_container_ready(app)
    live_terminal(app)

    send_last(
        app,
        f"GIT_TERMINAL_PROMPT=0 git clone {CLONE_URL} {CLONE_DIR}-2 "
        f"; echo {CANCEL_OK}\n",
    )
    app.wait_for_text("Sign in to 127.0.0.1", 60000)
    app.tap_button_exact("Cancel")
    # the dialog unwound on the app side (the buffer assert below
    # proves the container side: git's fatal + the marker echo)
    app.wait_gone("Waiting for authorization...", 15)

    buffer = buffer_until(
        app, lambda b: re.search(f"^{CANCEL_OK}$", b, re.M), timeout=90
    )
    assert "fatal" in buffer, buffer
