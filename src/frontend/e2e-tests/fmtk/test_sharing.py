"""fmtk e2e: the Sharing tab — role buckets, the advanced ACL editor,
and the permission visibility matrix (#3238).

Every user-visible sharing surface, driven through the real UI: the
tab's permission gate across the four fixture roles plus the two
constructed rows of the matrix (a ``share-workspace`` holder sees the
buckets without the Advanced editor; a ``share-advanced`` holder sees
the editor), the role buckets (the add-user dialog with its type-ahead,
the chip delete, and the member's next login reflecting the new
grants), the Advanced ACL editor (add entry, save, persistence across
a re-open, and a revoked Files tab for a second logged-in user), the
``join-workspace`` connect gate (a synthetic view-only permission set
dead-ends the workspace page before any tab renders), the step-up
interplay on a non-owner ACL edit (a wrong password re-prompts, the
correct one completes the save), and the admin icon tracking
``admins``-group membership across logins.

The permission dropdown's menu scrolls inside the dropdown overlay,
beyond the driver's reach — the Add-ACE dialog therefore keeps the
permission default ('view'), and grants that need other permissions are
seeded over the API and revoked through the editor's remove affordance
(a pure-UI path).

The fixture workspace ``fmtk-verify`` is mutated by the bucket and ACL
scenarios but every mutation is undone: the bucket add is chip-deleted,
and the ACL is snapshotted in wire form at scenario start and PUT back
in a ``finally``. The scratch workspace is run-unique and deleted by
the scenario chain that created it.

Scenarios run in definition order and share one chain: 1 drives the
fixture workspace as each of its four fixture roles; 2-3 drive it as
its owner (fmtk-admin) and the run-unique member; 4-6 chain one
run-unique scratch workspace through member-share → files revoke →
join-strip → step-up edit → delete; 7 is a standalone login. A
module-level sweep first clears what a hard-killed earlier run could
leak into the fixture workspace (its run-unique member out of the
buckets and the ACL). Re-run the whole file (a ``-k`` selection breaks
the chain — workspace names and users are run-unique).
"""

from __future__ import annotations

import time
import uuid

import pytest

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    find_label_nodes,
    find_nodes,
    http_api,
    http_login,
    node_type,
    parent_map,
    wait_for_fields,
)

RUN = uuid.uuid4().hex[:6]
FRESH_EMAIL = f"fmtk-shr{RUN}@example.com"
FRESH_PW = f"fmtk-Shr{RUN}!E5"
SHR_WS = f"fmtk-share-{RUN}"
COLLAB_EMAIL = "fmtk-collaborator@example.com"
CODER_EMAIL = "fmtk-coder@example.com"
SPECTATOR_EMAIL = "fmtk-spectator@example.com"


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


def register_fresh_user(harness, app) -> None:
    """Register + email-verify the run-unique member user; the
    auto-login lands on the empty owned list."""
    app.tap_label("Need an account? Create one")
    fields = wait_for_fields(app, "Create Account")
    app.enter_text(fields[0]["ref"], FRESH_EMAIL)
    app.enter_text(fields[1]["ref"], FRESH_PW)
    app.tap_label("Create Account")
    app.wait_for_text("Check your email to verify your account.")
    token = harness.smtp.token_for("verify", FRESH_EMAIL)
    app.navigate(f"/verify?token={token}")
    app.wait_for_text("No workspaces yet. Create one to get started.")


def open_fixture_workspace(app, email: str) -> None:
    """Open ``fmtk-verify``: the owner finds it under Owned by Me, role
    members tap the Shared with Me segment first. The workspace page's
    Terminal tab is the mount signal — retried, because a tap can race
    the page rebuild while a login settles."""
    for _ in range(3):
        app.navigate("/workspaces")
        if email != ADMIN_EMAIL:
            open_shared_segment(app)
        app.wait_for_text("fmtk-verify")
        app.tap_button_exact("fmtk-verify")
        try:
            app.wait_for_text("Terminal", 30000)
            return
        except FmtkError:
            continue
    raise FmtkError("fmtk-verify never opened")


def open_shared_segment(app) -> None:
    """Switch the workspaces list to the Shared with Me segment —
    waited, because a snapshot taken mid-route after a login can predate
    the segment bar's semantics."""
    app.wait_for_label("Shared with Me")
    app.tap_labeled_exact("Shared with Me")


def sharing_tab_present(app) -> bool:
    """The Sharing tab's exact label in the current tree."""
    return bool(find_label_nodes(app.snapshot(), "Sharing", exact=True))


def open_sharing_pane(app) -> None:
    """Tap the Sharing tab and wait for the buckets to render. A tab
    tap can race the workspace page's WS-driven rebuild while the page
    settles — re-tap until the pane actually mounts (the #3234-proven
    Settings-pane pattern)."""
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        app.tap_labeled_exact("Sharing")
        try:
            app.wait_for_label("Collaborators", 5)
            return
        except FmtkError:
            continue
    raise FmtkError("the Sharing pane never mounted")


def expand_advanced_editor(app) -> None:
    """Expand the Advanced ACL expander; the entries header is the
    mount signal."""
    app.tap_labeled_exact("Advanced: Access Control")
    app.wait_for_text("Access Control Entries")


def tap_overlay_labeled_exact(app, label: str) -> None:
    """Tap the LAST tappable node whose label carries ``label`` as a
    whole line.

    Dialogs and dropdown menus are route overlays that render after the
    page content, so when a label exists both behind and inside an
    overlay (the editor's ``Add`` button vs the Add-ACE dialog's, the
    buckets' ``view`` permission tag vs the permission dropdown), the
    overlay's instance is the last one in the tree.
    """
    tree = app.snapshot()
    hits = find_label_nodes(tree, label, exact=True)
    if not hits:
        raise FmtkError(f"no snapshot node labeled {label!r}")
    parents = parent_map(tree)
    node: dict | None = hits[-1]
    while node is not None:
        if "tap" in (node.get("actions") or []):
            return app.tap(node["ref"])
        node = parents.get(id(node))
    raise FmtkError(f"no tappable node above label {label!r}")


def wait_dialog_field(app, marker: str) -> str:
    """Wait for the dialog's marker text, then for its (only) text
    field; returns the ref. The marker can predate the dialog (a bucket
    add icon carries the same label the dialog title will), so the
    field's presence — not the marker's — is the real mount signal."""
    app.wait_for_text(marker)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        fields = find_nodes(app.snapshot(), lambda n: node_type(n) == "textField")
        if fields:
            return fields[0]["ref"]
        time.sleep(1)
    raise FmtkError(f"no text field appeared under {marker!r}")


def tap_search_result(app, email: str) -> None:
    """Tap the type-ahead result tile for ``email`` — the field itself
    also carries the typed text as its value, so the tile is matched as
    a tappable non-field node."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        hits = [
            node
            for node in find_label_nodes(app.snapshot(), email, exact=True)
            if node_type(node) != "textField" and "tap" in (node.get("actions") or [])
        ]
        if hits:
            return app.tap(hits[0]["ref"])
        time.sleep(1)
    raise FmtkError(f"no type-ahead result for {email!r}")


def wait_label_gone(app, label: str, timeout: float = 30) -> None:
    """Block until no node carries ``label`` (exact line)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_label_nodes(app.snapshot(), label, exact=True):
            return
        time.sleep(1)
    raise FmtkError(f"{label!r} never left the tree")


def add_ace_via_dialog(app, email: str) -> None:
    """Add one user-level ``Allow view`` ACE for ``email`` through the
    Add-ACE dialog. The permission dropdown's default ('view') is the
    target, so only the user picker is driven (see the module
    docstring). The picker's menu lists users newest-first, so the
    run-unique member is always its first item."""
    app.tap_button_exact("Add")
    app.wait_for_text("Add ACE")
    select_dialog_picker_value(app, "Select user", email)
    tap_overlay_labeled_exact(app, "Add")
    app.wait_gone("Add ACE")
    app.wait_for_text(email)  # the new entry row


def add_system_ace_via_dialog(app) -> None:
    """Add one system-level ``Allow view`` ACE (the Authenticated
    principal) through the Add-ACE dialog — the shape a NON-OWNER
    editor adds: the picker's user/group fetches hit admin-only
    listings and come back empty, so the System principal (no fetch)
    is the one the dialog can build."""
    app.tap_button_exact("Add")
    app.wait_for_text("Add ACE")
    select_dialog_picker_value(app, "User", "System")
    app.wait_for_text("Authenticated")  # the System-principal default
    tap_overlay_labeled_exact(app, "Add")
    app.wait_gone("Add ACE")
    app.wait_for_text("Authenticated")  # the new entry row


def select_dialog_picker_value(app, closed_label: str, item: str) -> None:
    """Pick ``item`` in the Add-ACE dialog's dropdown currently labeled
    ``closed_label`` (its hint or current value).

    An open dropdown menu REPLACES the dialog in the semantic tree, and
    a tap racing the menu's open animation can miss — so the loop
    re-reads the state (menu open? value chosen?) and drives whatever
    step is pending until the dialog is back with the value selected.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if app.has_text("Popup menu", 1000):
            app.tap_labeled_exact(item)  # a missed tap just retries
        else:
            try:
                app.tap_button_exact(closed_label)
            except FmtkError:
                pass  # the label left mid-race (the value just landed)
        time.sleep(1)
        if app.has_text("Add ACE", 2000) and app.has_text(item, 1000):
            return
    raise FmtkError(f"never selected {item!r} in the picker")


def save_acl(app) -> None:
    app.scroll_until_label("Save ACL")
    app.tap_button_exact("Save ACL")
    app.wait_for_text("Saved")


def remove_entry_via_editor(app, email: str, permission: str) -> None:
    """Remove one user-level entry row through the editor's remove
    affordance and save."""
    app.scroll_until_label(f"Remove entry {email} {permission}")
    app.tap_labeled_exact(f"Remove entry {email} {permission}")
    save_acl(app)


def wire_entry(user_id: str, permission: str) -> dict:
    """One user-level Allow entry in the PUT wire shape."""
    return {
        "action": 1,
        "principal_type": 1,
        "permission": permission,
        "user_id": user_id,
        "group_id": None,
        "system_principal": None,
    }


def resolved_to_wire(entries: list[dict]) -> list[dict]:
    """Resolved ACL rows -> the PUT wire shape (ids carried as-is)."""
    return [
        {
            "action": e["action"],
            "principal_type": e["principal_type"],
            "permission": e["permission"],
            "user_id": e.get("user_id"),
            "group_id": e.get("group_id"),
            "system_principal": e.get("system_principal"),
        }
        for e in entries
    ]


def owner_token(harness) -> str:
    return http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)


def user_id_by_email(harness, email: str) -> str:
    status, listing = harness.admin_api("GET", "/api/v1/users?page_size=200")
    assert status == 200, listing
    return next(u["id"] for u in listing["users"] if u["email"] == email)


def own_workspace_id(harness, name: str) -> str:
    token = owner_token(harness)
    status, mine = http_api(harness.backend.url, token, "GET", "/api/v1/workspaces")
    assert status == 200, mine
    return next(w["id"] for w in mine if w["name"] == name)


def get_workspace_acl(harness, ws_id: str) -> list[dict]:
    status, entries = http_api(
        harness.backend.url,
        owner_token(harness),
        "GET",
        f"/api/v1/workspaces/{ws_id}/acl",
    )
    assert status == 200, entries
    return entries


def put_workspace_acl(harness, ws_id: str, entries: list[dict]) -> None:
    status, body = http_api(
        harness.backend.url,
        owner_token(harness),
        "PUT",
        f"/api/v1/workspaces/{ws_id}/acl",
        entries,
    )
    assert status == 200, body


def fixture_role_members(harness, role: str) -> list[str]:
    """The fixture workspace's ``role`` bucket member emails (API)."""
    ws_id = own_workspace_id(harness, "fmtk-verify")
    status, roles = http_api(
        harness.backend.url,
        owner_token(harness),
        "GET",
        f"/api/v1/workspaces/{ws_id}/roles",
    )
    assert status == 200, roles
    bucket = next(r for r in roles if r["role"] == role)
    return [m["email"] for m in bucket["members"]]


def is_stale_fresh_row(entry: dict) -> bool:
    """A user-principal ACL row for some earlier run's run-unique
    member (every run-unique member shares the ``fmtk-shr`` email
    prefix)."""
    if entry.get("principal_type") != 1:
        return False
    principal = str(entry.get("principal") or "")
    return principal.startswith("fmtk-shr")


def sweep_stale_bucket_members(harness, ws_id: str) -> None:
    """Take a hard-killed earlier run's run-unique members out of the
    fixture workspace's role buckets."""
    status, roles = http_api(
        harness.backend.url,
        owner_token(harness),
        "GET",
        f"/api/v1/workspaces/{ws_id}/roles",
    )
    assert status == 200, roles
    for bucket in roles:
        stale = [m for m in bucket["members"] if str(m["email"]).startswith("fmtk-shr")]
        for member in stale:
            remove_bucket_member(harness, ws_id, bucket["role"], member["id"])


def sweep_stale_sharing_state(harness, ws_id: str) -> None:
    """Clear what a hard-killed earlier run can leak into the fixture
    workspace: its run-unique member out of the role buckets, and that
    run's member rows out of the ACL (the seed only ever adds, so
    without this every later run would carry — and re-PUT — the leak
    forever)."""
    sweep_stale_bucket_members(harness, ws_id)
    entries = get_workspace_acl(harness, ws_id)
    kept = [e for e in entries if not is_stale_fresh_row(e)]
    if len(kept) != len(entries):
        put_workspace_acl(harness, ws_id, resolved_to_wire(kept))


def seed_fresh_entries(harness, ws_id: str, permissions: list[str]) -> None:
    """Replace the run-unique member's user-level entries on the
    workspace with ``permissions``; every other entry (the owner
    wildcard, the role groups) stays as found."""
    entries = get_workspace_acl(harness, ws_id)
    fresh_id = user_id_by_email(harness, FRESH_EMAIL)
    kept = [e for e in entries if e.get("user_id") != fresh_id]
    put_workspace_acl(
        harness,
        ws_id,
        resolved_to_wire(kept) + [wire_entry(fresh_id, p) for p in permissions],
    )


def open_scratch_workspace(app) -> None:
    """Open the scratch workspace tile robustly (scroll + tap + retry
    the mount signal)."""
    for _ in range(3):
        app.navigate("/workspaces")
        app.wait_for_text(SHR_WS)
        app.scroll_until_label(SHR_WS)
        app.tap_button_exact(SHR_WS)
        try:
            app.wait_for_text("Terminal", 45000)
            return
        except FmtkError:
            continue
    raise FmtkError(f"{SHR_WS} never opened")


def delete_scratch_workspace(app) -> None:
    app.navigate("/workspaces")
    app.wait_for_text(SHR_WS)
    app.tap_label(f"Delete {SHR_WS}")
    app.wait_for_text("This will delete the workspace")
    app.tap_button_exact("Delete")
    app.wait_gone(SHR_WS)


def admin_group_id(harness) -> str:
    status, listing = harness.admin_api("GET", "/api/v1/groups?page_size=100")
    assert status == 200, listing
    return next(g["id"] for g in listing["groups"] if g["name"] == "admins")


def set_admin_group_member(harness, user_id: str, member: bool) -> None:
    """POST/DELETE the admins-group membership. A delete answered 404
    (not a member) is tolerated: the cleanup path runs unconditionally
    and must not mask the failure it follows."""
    path = f"/api/v1/groups/{admin_group_id(harness)}/members"
    if member:
        status, body = harness.admin_api("POST", path, {"user_id": user_id})
        assert status == 200, body
    else:
        status, _ = harness.admin_api("DELETE", f"{path}/{user_id}")
        assert status in (200, 404), status


# --- scenario-building blocks -------------------------------------------


def owner_opens_fixture_sharing(harness, app) -> None:
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    open_fixture_workspace(app, ADMIN_EMAIL)
    open_sharing_pane(app)


def fresh_opens_shared_workspace(
    app,
    name: str,
    mount_signal: str = "Terminal",
    gone_marker: str | None = None,
    expect_text: str = "No workspaces yet",
):
    """The run-unique member logs in, finds ``name`` under Shared with
    Me, and opens it. ``mount_signal`` is the text that proves the page
    rendered — Terminal for the member-share grants, Sharing for the
    share-* holders (whose synthetic grants carry no ``terminal``). A
    member whose grants mount NO tab at all (view/join only — the page
    renders an empty body, #2975) passes ``gone_marker``: a text that
    leaves when the list page does."""
    app.login(FRESH_EMAIL, FRESH_PW, expect_text=expect_text)
    for _ in range(3):
        open_shared_segment(app)
        app.wait_for_text(name)
        app.tap_button_exact(name)
        try:
            if gone_marker is not None:
                app.wait_gone(gone_marker, 45000)
            else:
                app.wait_for_text(mount_signal, 45000)
            return
        except FmtkError:
            app.navigate("/workspaces")
            continue
    raise FmtkError(f"{name} never opened for the member")


def patch_workspace_settings(harness, ws_id: str, patch: dict) -> None:
    status, body = http_api(
        harness.backend.url,
        owner_token(harness),
        "PATCH",
        f"/api/v1/workspaces/{ws_id}/settings",
        patch,
    )
    assert status == 200, body


@pytest.fixture(autouse=True, scope="module")
def fixture_workspace_never_idles(harness):
    """Pin the fixture workspace's idle timeout off for the suite.

    The sharing scenarios read the UI for long stretches between
    container touches — under the stock 300s idle timeout the reaper
    stops the container mid-scenario, the workspace page wedges on its
    reconnect overlay, and every later login/logout flow derails. The
    bag key is deleted on the way out (the deploy default resumes).
    Leaked state from a hard-killed earlier run is swept first (see
    ``sweep_stale_sharing_state``)."""
    ws_id = own_workspace_id(harness, "fmtk-verify")
    # stop a container left running by an earlier run: the idle pin is
    # read from the bag at container START, so a stale container would
    # keep the old 300s timeout
    http_api(
        harness.backend.url,
        owner_token(harness),
        "POST",
        f"/api/v1/workspaces/{ws_id}/stop",
    )
    patch_workspace_settings(harness, ws_id, {"idle_timeout": 0})
    sweep_stale_sharing_state(harness, ws_id)
    yield
    patch_workspace_settings(harness, ws_id, {"idle_timeout": None})


# --- scenarios ---------------------------------------------------------


def test_visibility_matrix_for_fixture_roles(harness, app):
    # the owner: the Sharing tab, every bucket with its fixture member
    # and grant tags, and the share-advanced-gated ACL editor
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    open_fixture_workspace(app, ADMIN_EMAIL)
    assert sharing_tab_present(app)
    open_sharing_pane(app)
    for role, member in (
        ("Owners", ADMIN_EMAIL),
        ("Collaborators", COLLAB_EMAIL),
        ("Coders", CODER_EMAIL),
        ("Spectators", SPECTATOR_EMAIL),
    ):
        assert find_label_nodes(app.snapshot(), role, exact=True), role
        assert app.has_text(member), member
    app.wait_for_text("All permissions")  # the owners bucket's wildcard
    assert find_label_nodes(app.snapshot(), "Advanced: Access Control", exact=True)
    expand_advanced_editor(app)
    # the bucket add icons coexist with the expanded editor (the
    # Save/Discard footer renders only while entries are dirty)
    assert find_label_nodes(app.snapshot(), "Add to owners", exact=True)
    app.logout()
    # the seeded role grants carry no share-* permission, so none of
    # the three role members gets the tab (the spectator keeps the
    # Terminal tab — it hosts the shared terminals they watch)
    for email in (COLLAB_EMAIL, CODER_EMAIL, SPECTATOR_EMAIL):
        at_login(harness, app)
        app.login(email, FIXTURE_PASSWORD, expect_text="No workspaces yet")
        open_fixture_workspace(app, email)
        assert not sharing_tab_present(app), email
        app.logout()


def owner_adds_member_to_bucket(harness, app) -> None:
    """The owner adds the run-unique member to the spectators bucket
    through the add dialog's type-ahead."""
    owner_opens_fixture_sharing(harness, app)
    app.tap_labeled_exact("Add to spectators")
    field = wait_dialog_field(app, "Add to spectators")
    app.enter_text(field, FRESH_EMAIL)
    # the result tile tap closes the dialog and adds the member; the
    # bucket icon carries the same label the dialog title did, so the
    # chip (the outcome) is the unambiguous mount signal
    tap_search_result(app, FRESH_EMAIL)
    app.wait_for_label(f"Remove {FRESH_EMAIL}")
    assert FRESH_EMAIL in fixture_role_members(harness, "spectators")


def owner_removes_member_from_bucket(harness, app) -> None:
    owner_opens_fixture_sharing(harness, app)
    app.tap_labeled_exact(f"Remove {FRESH_EMAIL}")
    wait_label_gone(app, f"Remove {FRESH_EMAIL}")
    assert FRESH_EMAIL not in fixture_role_members(harness, "spectators")


def remove_bucket_member(harness, ws_id: str, role: str, member_id: str) -> None:
    status, body = http_api(
        harness.backend.url,
        owner_token(harness),
        "DELETE",
        f"/api/v1/workspaces/{ws_id}/roles/{role}/{member_id}",
    )
    assert status == 200, body


def restore_spectators_bucket(harness) -> None:
    if FRESH_EMAIL not in fixture_role_members(harness, "spectators"):
        return
    remove_bucket_member(
        harness,
        own_workspace_id(harness, "fmtk-verify"),
        "spectators",
        user_id_by_email(harness, FRESH_EMAIL),
    )


def test_role_bucket_add_and_remove(harness, app):
    at_login(harness, app)
    register_fresh_user(harness, app)
    app.logout()
    assert FRESH_EMAIL not in fixture_role_members(harness, "spectators")
    try:
        # add: the member's next login sees the shared workspace open
        # (the spectators' join grant) but no Sharing tab (no share-*)
        owner_adds_member_to_bucket(harness, app)
        app.logout()
        fresh_opens_shared_workspace(app, "fmtk-verify")
        assert not sharing_tab_present(app)
        # remove: the chip leaves the bucket and the member's next
        # login sees the workspace leave the shared list
        app.logout()
        owner_removes_member_from_bucket(harness, app)
        app.logout()
        app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")
        open_shared_segment(app)
        app.wait_gone("fmtk-verify")
        app.logout()
    finally:
        restore_spectators_bucket(harness)


def share_holder_sees_buckets_without_editor(app) -> None:
    """The constructed matrix row: a share-workspace holder sees the
    buckets, but not the Advanced editor nor the role-write icons."""
    fresh_opens_shared_workspace(app, "fmtk-verify", mount_signal="Sharing")
    assert sharing_tab_present(app)
    open_sharing_pane(app)
    assert not find_label_nodes(app.snapshot(), "Advanced: Access Control", exact=True)
    assert not find_label_nodes(app.snapshot(), "Add to spectators", exact=True)
    app.logout()


def test_acl_editor_add_save_and_persisted_effect(harness, app):
    ws_id = own_workspace_id(harness, "fmtk-verify")
    seeded = resolved_to_wire(get_workspace_acl(harness, ws_id))
    try:
        # seed the share-workspace row of the matrix over the API (the
        # permission menu scrolls beyond the driver's reach — see the
        # module docstring); view/join let the member open the page
        seed_fresh_entries(
            harness, ws_id, ["view", "join-workspace", "share-workspace"]
        )
        # the editor: add an entry through the dialog (a second user-level
        # view row for the member — the picker's newest-first list makes
        # them its first item) and save
        owner_opens_fixture_sharing(harness, app)
        expand_advanced_editor(app)
        add_ace_via_dialog(app, FRESH_EMAIL)
        save_acl(app)
        # persistence: re-open the editor — the row is still there and
        # the API carries both member view entries
        app.navigate("/workspaces")
        open_fixture_workspace(app, ADMIN_EMAIL)
        open_sharing_pane(app)
        expand_advanced_editor(app)
        app.scroll_until_label(f"Remove entry {FRESH_EMAIL} view")
        fresh_id = user_id_by_email(harness, FRESH_EMAIL)
        entries = get_workspace_acl(harness, ws_id)
        views = [
            e
            for e in entries
            if e.get("user_id") == fresh_id and e["permission"] == "view"
        ]
        assert len(views) == 2, entries
        # the matrix row holds: buckets without the editor...
        app.logout()
        share_holder_sees_buckets_without_editor(app)
        # ...and revoking the grant through the editor takes the tab
        # away on the member's next login (view/join remain: the
        # workspace itself stays shared and openable)
        owner_opens_fixture_sharing(harness, app)
        expand_advanced_editor(app)
        remove_entry_via_editor(app, FRESH_EMAIL, "share-workspace")
        app.logout()
        # view/join remain: the page still opens, but with NO tab at all
        # (the empty-body #2975 shape) — the list page's create FAB
        # leaving is the mount signal
        fresh_opens_shared_workspace(app, "fmtk-verify", gone_marker="Create workspace")
        assert not sharing_tab_present(app)
        app.logout()
    finally:
        put_workspace_acl(harness, ws_id, seeded)


def test_member_share_and_files_revoke(harness, app):
    # the scratch chain starts: a member share grants the fixed block
    # (view/monitor/join/terminal/files-*), so the Files tab mounts
    token = owner_token(harness)
    status, body = http_api(
        harness.backend.url,
        token,
        "POST",
        "/api/v1/workspaces",
        # idle off: the chain's reads must not idle-stop the container
        {"name": SHR_WS, "settings": {"idle_timeout": 0}},
    )
    assert status in (200, 201), body
    ws_id = own_workspace_id(harness, SHR_WS)
    status, body = http_api(
        harness.backend.url,
        token,
        "POST",
        f"/api/v1/workspaces/{ws_id}/members",
        {"email": FRESH_EMAIL},
    )
    assert status == 200, body
    at_login(harness, app)
    fresh_opens_shared_workspace(app, SHR_WS)
    assert find_label_nodes(app.snapshot(), "Files", exact=True)
    # the owner revokes files-view through the editor; the member's
    # next login mounts no Files tab (the #2886 mount gate)
    app.logout()
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    open_scratch_workspace(app)
    open_sharing_pane(app)
    expand_advanced_editor(app)
    remove_entry_via_editor(app, FRESH_EMAIL, "files-view")
    app.logout()
    fresh_opens_shared_workspace(app, SHR_WS)
    assert not find_label_nodes(app.snapshot(), "Files", exact=True)
    app.logout()


def test_join_gate_denies_without_permission(harness, app):
    # the synthetic permission set: exactly one user-level Allow view —
    # the shared list still offers the tile, but the connect gate
    # (join-workspace) refuses with the stamped forbidden code and the
    # page dead-ends on the access-revoked view (#2891) before any tab
    seed_fresh_entries(harness, own_workspace_id(harness, SHR_WS), ["view"])
    at_login(harness, app)
    app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")
    open_shared_segment(app)
    app.wait_for_text(SHR_WS)
    app.tap_button_exact(SHR_WS)
    app.wait_for_text("Access to this workspace has been revoked", 60000)
    app.wait_for_text("Permission denied")  # the refusal detail line
    assert not find_label_nodes(app.snapshot(), "Terminal", exact=True)
    # the revoked view's app bar carries no logout action — leave via
    # its back button's destination first
    app.navigate("/workspaces")
    app.logout()


def test_step_up_on_non_owner_acl_edit(harness, app):
    # seed the full sharing powers for the non-owner member (the
    # takeover-class write now needs a fresh sign-in)
    seed_fresh_entries(
        harness,
        own_workspace_id(harness, SHR_WS),
        ["view", "join-workspace", "share-workspace", "share-advanced"],
    )
    original = harness.config.get("step_up_window_minutes", "0")
    try:
        # step_up_window_minutes is not surfaced on /api/v1/config — the
        # dialog itself is the behavioral verification. The arm sits
        # inside the try so a failed swap cannot leave the window armed
        # with no restore (the finally runs either way).
        harness.backend.swap_settings(
            {"step_up_window_minutes": "60"}, apply="sighup", verify=False
        )
        at_login(harness, app)
        fresh_opens_shared_workspace(app, SHR_WS, mount_signal="Sharing")
        open_sharing_pane(app)
        expand_advanced_editor(app)
        # the non-owner editor's pickers cannot fetch users/groups (the
        # listings are admin-only), so the entry it can build is a
        # System-principal one
        add_system_ace_via_dialog(app)
        # the save is refused with step_up_required -> the sudo dialog;
        # a wrong password re-prompts without retrying the write
        app.scroll_until_label("Save ACL")
        app.tap_button_exact("Save ACL")
        app.wait_for_text("Re-authentication required")
        app.enter_text(app.ref_for_label("Password", "textField"), "definitely-wrong")
        app.tap_label("Confirm")
        app.wait_for_text("Incorrect password — try again.")
        app.enter_text(app.ref_for_label("Password", "textField"), FRESH_PW)
        app.tap_label("Confirm")
        app.wait_gone("Re-authentication required")
        app.wait_for_text("Saved")
        # the elevated write landed: the entry persists server-side
        entries = get_workspace_acl(harness, own_workspace_id(harness, SHR_WS))
        assert any(
            e["principal_type"] == 0 and e["permission"] == "view" for e in entries
        ), entries
        app.logout()
    finally:
        harness.backend.swap_settings(
            {"step_up_window_minutes": original}, apply="sighup", verify=False
        )
    # the scratch chain ends: the owner deletes the workspace
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    delete_scratch_workspace(app)
    app.logout()


def test_admin_icon_tracks_group_membership(harness, app):
    fresh_id = user_id_by_email(harness, FRESH_EMAIL)
    at_login(harness, app)
    app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")
    assert not find_label_nodes(app.snapshot(), "Admin", exact=True)
    app.logout()
    try:
        set_admin_group_member(harness, fresh_id, member=True)
        app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")
        app.wait_for_label("Admin")
        app.logout()
    finally:
        set_admin_group_member(harness, fresh_id, member=False)
    app.login(FRESH_EMAIL, FRESH_PW, expect_text="No workspaces yet")
    wait_label_gone(app, "Admin")
    app.logout()
