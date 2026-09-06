"""Smoke scenario for the fmtk e2e harness (#3232 "done when").

One ordered scenario — the phases share app/server state, so they live in
a single test:

1. the debug app boots to the login surface (backend + proxy + seed +
   flutter run all came up through ``Harness.boot``);
2. a mid-run settings swap lands via SIGHUP (#1587) — a run-unique login
   banner title changes on the live server, and a fresh app boot renders
   it in the UI as the banner consent dialog (run-unique so a re-run
   against the kept backend stays valid);
3. dismissing the banner and logging in as the fixture owner lands on
   /workspaces with fmtk-verify under "Owned by Me";
4. a full server restart (config rewrite + TERM + relaunch) keeps the DB —
   after re-login the workspace is still there;
5. zero uncaught app errors across the whole scenario (conftest drain).
"""

from __future__ import annotations

import uuid

from fmtkharness import ADMIN_EMAIL, FIXTURE_PASSWORD

BANNER_TITLE = f"fmtk e2e swap {uuid.uuid4().hex[:8]}"
BANNER_BODY = "swapped live over SIGHUP"


def test_harness_smoke(harness, app):
    original_title = harness.config.get("login_banner_title", "")
    original_banner = harness.config.get("login_banner", "")
    try:
        _smoke_phases(harness, app)
    finally:
        # The banner persists in the adopted backend's config; a leftover
        # consent dialog would mask the login form for the next suite's
        # logins (each suite only dismisses the banner interactively).
        harness.backend.swap_settings(
            {
                "login_banner_title": original_title,
                "login_banner": original_banner,
            },
            apply="sighup",
            verify=False,
        )


def _smoke_phases(harness, app) -> None:
    # --- phase 1: app booted to the login surface -----------------------
    app.wait_for_login_page()

    # --- phase 2: settings swap + SIGHUP lands in the UI ----------------
    harness.backend.swap_settings(
        {"login_banner_title": BANNER_TITLE, "login_banner": BANNER_BODY},
        apply="sighup",
    )
    harness.restart_app()  # fresh main() re-fetches config -> banner renders
    app.wait_for_login_page()
    app.wait_for_text(BANNER_TITLE)
    app.wait_for_text(BANNER_BODY)

    # --- phase 3: login as the fixture owner -> Owned by Me -------------
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    app.wait_for_text("Owned by Me")

    # --- phase 4: full server restart keeps state -----------------------
    harness.backend.restart()
    harness.restart_app()
    app.wait_for_login_page()
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    app.wait_for_text("Owned by Me")
