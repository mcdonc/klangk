"""fmtk e2e: feature-plugin tabs — features_enable swaps (#3243).

The feature-plugin system gates UI registration on the deploy's
``features_enable`` list: a shipped-but-inactive feature's app-bar
icon, overlay, routes, and dispatched tools never register. This
suite verifies the enable/disable lifecycle by swapping the list
and restarting the app (features are resolved once at boot).

Soliplex is the primary test vehicle because it has a visible app-bar
icon ("Soliplex servers") that is deterministically present when
enabled and absent when disabled. The other features (beep, boingball,
celebrate, browser-fetch) are tool-only or overlay-on-demand with no
persistent UI element, so their presence is verified via the API's
``features_enable`` contract rather than UI assertions.

The ``git-credential`` feature requires a live git command in the
terminal to trigger the credential dialog — that scenario is deferred
to the git-credential-specific suite if one exists.
"""

from __future__ import annotations

import time

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
)


# --- shared driving helpers ------------------------------------------------


def at_login(harness, app) -> None:
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


# --- scenarios -------------------------------------------------------------


def test_features_enable_api_reflects_setting(harness, app):
    """The ``/api/v1/config`` endpoint reflects the ``features_enable``
    setting: present and matching when set, absent/empty when cleared."""
    try:
        harness.backend.swap_settings(
            {"features_enable": "beep,celebrate"},
            apply="restart",
            verify=False,
        )
        config = harness.backend.api_config()
        assert "features_enable" in config, "features_enable key missing"
        assert "beep" in config["features_enable"]
        assert "celebrate" in config["features_enable"]
        assert "soliplex" not in config["features_enable"]
    finally:
        _clear_features_enable(harness)


def _clear_features_enable(harness) -> None:
    """Remove ``features_enable`` from the config so the server defaults
    to ``None`` (use manifest defaults)."""
    harness.backend.config.pop("features_enable", None)
    harness.backend.swap_settings({}, apply="restart", verify=False)
    # wait for the config endpoint to reflect the absence
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        config = harness.backend.api_config()
        fe = config.get("features_enable")
        if fe is None:
            return
        time.sleep(1)
    assert False, f"features_enable not cleared after 15s: {fe!r}"


def test_features_enable_swap_roundtrip(harness, app):
    """Swapping ``features_enable`` and restarting the server makes the
    ``/api/v1/config`` response reflect the new list."""
    try:
        harness.backend.swap_settings(
            {"features_enable": "git-credential"},
            apply="restart",
            verify=False,
        )
        config = harness.backend.api_config()
        assert "git-credential" in config.get("features_enable", "")

        # swap to a different list
        harness.backend.swap_settings(
            {"features_enable": "beep,boingball"},
            apply="restart",
            verify=False,
        )
        config = harness.backend.api_config()
        fe = config.get("features_enable", "")
        assert "beep" in fe
        assert "boingball" in fe
        assert "git-credential" not in fe
    finally:
        _clear_features_enable(harness)


def test_features_enable_app_boots_with_custom_set(harness, app):
    """An app booted with ``features_enable`` set resolves that exact
    list (verified via the app's config fetch). The frontend can't be
    inspected for feature UI (features are stubbed in dev builds), so
    this asserts the server-side contract that the app reads at boot."""
    try:
        harness.backend.swap_settings(
            {"features_enable": "celebrate,beep"},
            apply="restart",
            verify=False,
        )
        # restart so the app re-fetches config at boot
        harness.restart_app()
        app.wait_for_login_page()
        app.dismiss_login_banner()

        # verify the server's config still carries the list
        config = harness.backend.api_config()
        fe = config.get("features_enable", "")
        assert "celebrate" in fe
        assert "beep" in fe

        # the app loads and is usable (no crash from the features_enable)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text=ADMIN_EMAIL)
        app.logout()
    finally:
        _clear_features_enable(harness)
        harness.restart_app()


def test_feature_tab_recreated_across_workspace_close_reopen(harness, app):
    """#3409: feature-tab instances are per-workspace-page.

    Boingball (dormant by default) carries ``BoingTab`` — a
    ``WorkspaceTabPlugin`` mixing in ``ChangeNotifier`` (disposable
    state). Enable it, then within ONE app session: open the fixture
    workspace, exercise the tab (notifyListeners + live badge), close
    the workspace (the page disposes its tab set — terminal), and open
    the workspace again. The reopened page must build a FRESH tab
    instance (the per-page bounce counter reset to zero proves it), and
    no used-after-being-disposed error may escape (the post-test error
    drain fails the run if one does)."""
    try:
        harness.backend.swap_settings(
            {"features_enable": "beep,boingball"},
            apply="restart",
            verify=False,
        )
        harness.restart_app()
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")

        # --- first open: fresh tab instance, disposable state exercised ---
        app.navigate("/workspaces")
        app.wait_for_text("fmtk-verify")
        app.tap_button_exact("fmtk-verify")
        app.wait_for_text("Terminal", 30000)
        app.tap_labeled_exact("Boing")
        app.wait_for_text("Boing bounces: 0")
        app.tap_label("Do a boing")
        app.wait_for_text("Boing bounces: 1")

        # --- close the workspace page: its tab set is disposed (terminal) ---
        app.navigate("/workspaces")
        app.wait_for_text("fmtk-verify")

        # --- second open in the same session: a FRESH instance serves the
        # page (counter back to zero) — reuse of the disposed tab would
        # either keep the counter or throw used-after-being-disposed. ---
        app.tap_button_exact("fmtk-verify")
        app.wait_for_text("Terminal", 30000)
        app.tap_labeled_exact("Boing")
        app.wait_for_text("Boing bounces: 0")
    finally:
        _clear_features_enable(harness)
        harness.restart_app()
