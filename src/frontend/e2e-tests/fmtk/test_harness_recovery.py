"""The dwds isolate-wedge recovery policy (#3231).

A tab that leaves the app origin and returns can leave the flutter
tool's debug session permanently without a Flutter isolate (the
2026-09-09 nightly: every test after the unverified-email OIDC
scenario parked the tab on the backend's JSON error page failed on
"No Flutter isolate found"). ``FmtkClient.exec`` carries the recovery:
a gone isolate with the tab on the app origin arms a marker, a state
that outlives the window restarts the flutter run transparently, and a
parked-away tab never arms. This scenario pins all three legs against
the live stack.

The 2026-09-10+ nightlies exposed a second leg (#3469): the first
scenario to meet the dead instance reaches for
``Harness.restart_app()`` — the designated recovery — whose pre-restart
drain died on the same gone isolate, so the restart never ran and every
following scenario failed on the dead connection (the flows group's
nightly cascade). ``restart_app`` now treats a gone-isolate drain as an
empty drain and relaunches; the scenario below pins that policy
(including what must still raise) against stubbed drains.
"""

from __future__ import annotations

import time

import pytest

from fmtkharness import (
    BACKEND_PORT,
    FmtkClient,
    FmtkError,
    FlutterRun,
    ISOLATE_GONE_MARK,
    WEDGE_RECOVERY_SECONDS,
    cdp_eval,
    cdp_wait_tab_url,
    proxy_origin,
)


def gone_error() -> FmtkError:
    """The envelope failure a gone isolate produces."""
    return FmtkError(
        "fmtk semantic_snapshot printed no JSON envelope "
        f"(stderr: Unhandled exception:\nBad state: {ISOLATE_GONE_MARK})"
    )


def at_login(harness, app) -> None:
    """Land on the usable login form from any prior state — the core
    suite runs the smoke scenario first and it ends logged in (#3413) —
    then dismiss any leftover login-banner dialog."""
    app.ensure_tab_at_app()
    app.navigate("/login")
    if not app.has_text("Log In", 5000):
        try:
            app.dart_logout()
        except FmtkError:
            harness.restart_app()
    app.wait_for_login_page()
    app.dismiss_login_banner()
    app.wait_for_text("Email or handle")


def test_isolate_wedge_recovery(harness, app):
    at_login(harness, app)

    # the tab is on the app origin with the isolate attached: a gone
    # sighting arms the marker and keeps declining inside the window
    # (the ordinary dwds re-attach race must not restart the run)
    app.flutter.isolate_gone_since = None
    assert app.recover_if_wedged(gone_error()) is False
    armed = app.flutter.isolate_gone_since
    assert armed is not None
    assert app.recover_if_wedged(gone_error()) is False
    assert app.flutter.isolate_gone_since == armed

    # a parked-away tab legitimately has no isolate — backdated beyond
    # the window, the sighting still declines AND clears the marker
    # (the SSO chain parks the tab at the IdP mid-scenario; that state
    # is the caller's to drive, not a wedge)
    app.flutter.isolate_gone_since = armed - (WEDGE_RECOVERY_SECONDS + 5)
    cdp_eval(f"location.href='http://127.0.0.1:{BACKEND_PORT}/api/v1/config'")
    cdp_wait_tab_url((f"http://127.0.0.1:{BACKEND_PORT}/",), timeout=30)
    time.sleep(3)  # the document load unloads the running app
    assert app.recover_if_wedged(gone_error()) is False
    assert app.flutter.isolate_gone_since is None

    # back on the app origin with the window outlived: recovery runs —
    # a real flutter restart — and the client drives the fresh app
    # (wait for the URL to commit first: the navigation is async, and
    # a pre-commit sighting reads as parked-away and clears the marker)
    cdp_eval(f"location.href='{proxy_origin()}/#/login'")
    cdp_wait_tab_url((proxy_origin(),), timeout=30)
    app.flutter.isolate_gone_since = time.monotonic() - (WEDGE_RECOVERY_SECONDS + 5)
    assert app.recover_if_wedged(gone_error()) is True
    app.wait_for_login_page()


def test_restart_app_survives_a_dead_isolate(harness, monkeypatch):
    """The pre-restart drain must tolerate the state it recovers
    (#3469): a gone isolate counts as an empty drain and the
    stop/relaunch proceeds, while a live drain's real errors and any
    other drain failure still raise."""
    relaunched = []
    monkeypatch.setattr(FlutterRun, "stop", lambda self: None)
    monkeypatch.setattr(
        FlutterRun, "launch", lambda self, url_suffix="": relaunched.append(url_suffix)
    )

    # a dead isolate's drain (the nightly cascade's exact failure):
    # restart_app relaunches instead of raising, and clears the wedge
    # marker the outgoing instance armed
    def gone_drain(self):
        raise gone_error()

    monkeypatch.setattr(FmtkClient, "app_errors", gone_drain)
    harness.flutter.isolate_gone_since = time.monotonic()
    harness.restart_app()
    assert relaunched == [""]
    assert harness.flutter.isolate_gone_since is None

    # a drain with real errors still fails the test before any stop
    monkeypatch.setattr(FmtkClient, "app_errors", lambda self: [{"message": "boom"}])
    with pytest.raises(FmtkError, match="app errors before restart_app"):
        harness.restart_app()

    # a drain failure that is not the gone-isolate signature raises —
    # a broken drain must not quietly become a restart
    def broken_drain(self):
        raise FmtkError("fmtk get_app_errors failed: toolkit down (None)")

    monkeypatch.setattr(FmtkClient, "app_errors", broken_drain)
    with pytest.raises(FmtkError, match="toolkit down"):
        harness.restart_app()
    assert relaunched == [""]  # no further stop/launch ran
