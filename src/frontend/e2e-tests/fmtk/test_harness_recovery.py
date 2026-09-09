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
"""

from __future__ import annotations

import time

from fmtkharness import (
    BACKEND_PORT,
    FmtkError,
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


def test_isolate_wedge_recovery(harness, app):
    app.wait_for_login_page()

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
