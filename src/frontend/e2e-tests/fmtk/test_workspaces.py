"""fmtk e2e: workspace list, create/import, start/stop/restart, delete
(#3234).

Every user-visible workspace-lifecycle surface, driven through the real
UI: the list page's Owned/Shared sections, creation (dialog + FAB) and
the container reaching ``running``, import from an archive over the
import dialog's file hook, stop / start / restart transitions (settings
Danger Zone + the stopped overlay + the pending-restart notice) with the
list status label tracking the real container state throughout, delete
disappearing for the owner and for shared members, the per-user-home
file-browser rooting (the per-handle-home ceiling swapped on over
SIGHUP), the swapped default image picked up by the create dialog, and
the idle auto-stop reaper stopping a workspace in-test.

Tests run in definition order and share one ordered chain: 2-6 drive
one run-unique workspace through create → open → terminal → stop →
restart → settings-restart → delete as the run-registered user; the
later scenarios are self-contained logins. Re-run the whole file (a
``-k`` selection breaks the chain — workspace names and users are
run-unique).

The fixture workspace ``fmtk-verify`` is never mutated or deleted; every
scratch workspace is run-unique and cleaned up by the scenario that
created it.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import time
import uuid

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    find_nodes,
    http_api,
    http_login,
    node_labels,
    node_type,
)

RUN = uuid.uuid4().hex[:6]
FRESH_EMAIL = f"fmtk-ws{RUN}@example.com"
FRESH_PW = f"fmtk-Ws{RUN}!E5"
LIFE_WS = f"fmtk-life-{RUN}"
IDLE_WS = f"fmtk-idle-{RUN}"
HOME_WS = f"fmtk-home-{RUN}"
IMP_WS = f"fmtk-imp-{RUN}"
DEL_WS = f"fmtk-del-{RUN}"
COLLAB_EMAIL = "fmtk-collaborator@example.com"
# A second real tag of the workspace image: swapping the default must
# point somewhere startable, not just anywhere.
SWAP_IMAGE = "localhost/klangk-workspace:2026.09.05-7422a6cfd"


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


def login_fresh(app) -> None:
    app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")


def register_fresh_user(harness, app) -> None:
    """Register + email-verify the run-unique user; the auto-login lands
    on the empty owned list (the issue's Done-when starting point)."""
    app.tap_label("Need an account? Create one")
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], FRESH_EMAIL)
    app.enter_text(fields[1]["ref"], FRESH_PW)
    app.tap_label("Create Account")
    app.wait_for_text("Check your email to verify your account.")
    token = harness.smtp.token_for("verify", FRESH_EMAIL)
    app.navigate(f"/verify?token={token}")
    app.wait_for_text("No workspaces yet. Create one to get started.")


def create_workspace(app, name: str, idle: str | None = None) -> None:
    """Create via the create FAB + dialog (``idle`` fills the Idle
    Timeout field); the dialog closing and the name listing are the
    assertions."""
    app.tap_identifier("create-workspace-fab")
    app.wait_for_text("New Workspace")
    app.enter_text_identifier("create-workspace-name", name)
    if idle is not None:
        app.enter_text_identifier("create-workspace-idle-timeout", idle)
    app.tap_label("Create")
    app.wait_gone("New Workspace")
    app.wait_for_text(name)


def open_workspace(app, name: str) -> None:
    """Tap the tile; the workspace page's Terminal tab (absent from the
    list page) is the mount signal."""
    app.navigate("/workspaces")
    app.wait_for_text(name)
    app.tap_labeled_exact(name)
    app.wait_for_text("Terminal", 60000)


def terminal_marker(app, marker: str, timeout: float = 150) -> None:
    """Prove the workspace is usable: execute an echo and find it in the
    buffer. sendText's return proves nothing (AGENTS.md) — only the
    buffer read does. Self-healing across the states a lifecycle test
    leaves the pane in: an unmounted terminal (another tab was active,
    or the pane was replaced) remounts via the Terminal tab, and a
    stop/restart still in flight simply retries until container_ready.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buffer = app.terminal_buffer()
        if marker in buffer:
            return
        if "NO-TERMINAL-STATE" in buffer:
            app.tap_labeled_exact("Terminal")
        app.terminal_send(f"echo {marker}")
        time.sleep(3)
    raise AssertionError(f"terminal never echoed {marker!r}")


def own_workspace_id(harness, email: str, password: str, name: str) -> int:
    token = http_login(harness.backend.url, email, password)
    status, mine = http_api(harness.backend.url, token, "GET", "/api/v1/workspaces")
    assert status == 200, mine
    return next(w["id"] for w in mine if w["name"] == name)


def delete_from_list(app, name: str) -> None:
    """Delete via the tile's trailing button + confirm dialog; the name
    leaving the list is the assertion."""
    app.navigate("/workspaces")
    app.wait_for_text(name)
    app.tap_label(f"Delete {name}")
    app.wait_for_identifier("workspace-delete-confirm")
    app.tap_identifier("workspace-delete-confirm")
    app.wait_gone(name)


def import_archive_bytes(harness) -> bytes:
    """A minimal valid import archive: workspace.json carrying THIS
    instance's id (provenance is checked) — the home/ tree is optional
    and omitted."""
    instance_id = harness.backend.config["data_dir"].rstrip("/") + "/instance-id"
    with open(instance_id) as fh:
        local_id = fh.read().strip()
    meta = json.dumps({"name": "archived", "instance_id": local_id}).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        entry = tarfile.TarInfo("workspace.json")
        entry.size = len(meta)
        tar.addfile(entry, io.BytesIO(meta))
    return buf.getvalue()


# --- scenarios ---------------------------------------------------------


def test_list_sections_owner_and_shared(harness, app):
    # the fixture owner sees fmtk-verify under "Owned by Me"
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    app.wait_for_text("Owned by Me")
    app.wait_for_text("Shared with Me")
    app.logout()
    # a role member sees it under "Shared with Me" after tapping the tab
    app.login(COLLAB_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
    app.tap_labeled_exact("Shared with Me")
    app.wait_for_text("fmtk-verify")
    app.wait_for_text("fmtk-admin@example.com")  # shared tiles show the owner
    app.logout()


def test_fresh_user_registers_and_creates_workspace(harness, app):
    at_login(harness, app)
    register_fresh_user(harness, app)
    create_workspace(app, LIFE_WS)
    # a fresh workspace's container has never started
    app.wait_for_label("Workspace status: stopped")


def test_open_workspace_terminal_works(harness, app):
    open_workspace(app, LIFE_WS)
    terminal_marker(app, f"OPEN{RUN}")
    # the tile tracks the real container state once it runs
    app.navigate("/workspaces")
    app.wait_for_label("Workspace status: running")


def test_stop_and_restart_from_overlay(harness, app):
    open_workspace(app, LIFE_WS)
    # stop: Settings -> Danger Zone -> Shut Down Container -> confirm
    app.tap_labeled_exact("Settings")
    app.wait_for_label("General")
    app.scroll_until_label("Shut Down Container")
    app.tap_identifier("shutdown-container")
    app.wait_for_identifier("shutdown-confirm")
    app.tap_identifier("shutdown-confirm")
    app.wait_for_identifier("container-stopped-overlay", 60)
    # start again from the overlay: spinner, then container_ready clears
    # it and the workspace is usable again (the stop -> start cycle)
    app.tap_identifier("container-restart-button")
    app.wait_identifier_gone("container-stopped-overlay", 120)
    terminal_marker(app, f"RST{RUN}")
    app.navigate("/workspaces")
    app.wait_for_label("Workspace status: running")


def test_settings_edit_restarts_via_notice(harness, app):
    open_workspace(app, LIFE_WS)
    app.tap_labeled_exact("Settings")
    app.wait_for_label("General")
    app.scroll_until_label("Idle Timeout (s)")
    app.enter_text_identifier("settings-idle-timeout", "0")
    app.scroll_until_label("Save")
    app.tap_label("Save")
    # a create-time edit arms the pending-restart notice with its action
    app.wait_for_text("Restart the workspace to apply")
    app.tap_label("Restart now")
    # the running container restarts (no overlay between states here) —
    # the terminal coming back is the proof
    terminal_marker(app, f"SVR{RUN}")


def test_delete_workspace(harness, app):
    delete_from_list(app, LIFE_WS)
    app.logout()


def test_import_from_archive(harness, app):
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    archive = base64.b64encode(import_archive_bytes(harness)).decode()
    # hand the archive to the dialog's visible-for-testing picker hook —
    # the browser's native file chooser cannot be driven from the harness
    app.exec(
        "evaluate_dart_expression",
        {
            "expression": (
                "testPickFileBytesOverride = ({String accept = '*'}) "
                f"async => base64Decode('{archive}');"
            ),
            "libraryUri": (
                "package:klangk_frontend/workspace/import_workspace_dialog.dart"
            ),
        },
    )
    app.tap_identifier("import-workspace-fab")
    app.wait_for_text("Import Workspace")
    app.tap_identifier("import-select-file")
    app.wait_for_text("workspace.tar.gz")  # the picked file rendered
    app.enter_text_identifier("import-workspace-name", IMP_WS)
    app.tap_label("Import")
    app.wait_gone("Import Workspace")
    app.wait_for_text(IMP_WS)
    # the imported workspace opens like any other
    open_workspace(app, IMP_WS)
    delete_from_list(app, IMP_WS)
    app.logout()


def test_per_user_home_roots_file_browser(harness, app):
    original = harness.config.get("per_handle_home", False)
    harness.backend.swap_settings({"per_handle_home": True}, apply="sighup")
    harness.backend.wait_config_value("per_handle_home_available", True)
    try:
        at_login(harness, app)
        login_fresh(app)
        # the create flow re-fetches deploy config (#2994): the per-handle
        # checkbox now defaults on, so an untouched form opts the new
        # workspace in
        create_workspace(app, HOME_WS)
        open_workspace(app, HOME_WS)
        app.tap_labeled_exact("Files")
        node = app.wait_for_identifier("file-browser-path")
        label = " ".join(node_labels(node))
        # rooted at the member's per-handle home, not the shared
        # /home/klangk (the per-user-home Playwright spec's assertion,
        # driven through the UI this time)
        assert "/home/" in label, label
        assert "/home/klangk" not in label, label
        delete_from_list(app, HOME_WS)
        app.logout()
    finally:
        harness.backend.swap_settings({"per_handle_home": original}, apply="sighup")
        harness.backend.wait_config_value("per_handle_home_available", False)


def test_swapped_default_image_picked_up(harness, app):
    original = harness.config.get("image_name", "klangk-workspace")
    harness.backend.swap_settings(
        {"image_name": SWAP_IMAGE}, apply="sighup", verify=False
    )
    try:
        # /config does not expose image_name — the picker endpoint does
        token = http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)
        status, images = http_api(harness.backend.url, token, "GET", "/api/v1/images")
        assert status == 200 and images["default"] == SWAP_IMAGE, images
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
        app.tap_identifier("create-workspace-fab")
        app.wait_for_text("New Workspace")
        # the dialog preselects the swapped default; a workspace created
        # from this form resolves to it at start
        app.wait_for_text(SWAP_IMAGE)
        app.tap_label("Cancel")
        app.wait_gone("New Workspace")
        app.logout()
    finally:
        harness.backend.swap_settings(
            {"image_name": original}, apply="sighup", verify=False
        )


def test_idle_auto_stop(harness, app):
    at_login(harness, app)
    login_fresh(app)
    # 15s idle timeout; the reaper sweeps at half the smallest active
    # timeout, so the stop lands within ~25s of the last input
    create_workspace(app, IDLE_WS, idle="15")
    open_workspace(app, IDLE_WS)
    terminal_marker(app, f"IDL{RUN}")
    # no further input: the overlay carries the idle reason, and the
    # list status label tracks the stop
    app.wait_for_identifier("container-stopped-overlay", 90)
    app.wait_for_text("idle timeout")
    app.navigate("/workspaces")
    app.wait_for_label("Workspace status: stopped")
    delete_from_list(app, IDLE_WS)
    app.logout()


def test_delete_disappears_for_shared_member(harness, app):
    at_login(harness, app)
    login_fresh(app)
    create_workspace(app, DEL_WS)
    # share with the fixture collaborator over the owner API (the
    # Sharing UI is #3238's surface)
    token = http_login(harness.backend.url, FRESH_EMAIL, FRESH_PW)
    ws_id = own_workspace_id(harness, FRESH_EMAIL, FRESH_PW, DEL_WS)
    status, body = http_api(
        harness.backend.url,
        token,
        "POST",
        f"/api/v1/workspaces/{ws_id}/members",
        {"email": COLLAB_EMAIL},
    )
    assert status == 200, body
    # the member sees it under Shared with Me
    app.logout()
    app.login(COLLAB_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
    app.tap_labeled_exact("Shared with Me")
    app.wait_for_text(DEL_WS)
    app.logout()
    # the owner deletes; it leaves the member's shared list too
    app.login(FRESH_EMAIL, FRESH_PW, expect_text=DEL_WS)
    delete_from_list(app, DEL_WS)
    app.logout()
    app.login(COLLAB_EMAIL, FIXTURE_PASSWORD, expect_text="No workspaces yet")
    app.tap_labeled_exact("Shared with Me")
    app.wait_gone(DEL_WS)
    app.logout()
