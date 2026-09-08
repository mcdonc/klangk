"""fmtk e2e: the admin UI — users, groups, invitations, events, server
schedule (#3240).

Every admin surface behind the app-bar admin icon, driven through the
real UI as the fixture admin (``fmtk-admin``, an ``admins``-group
member): the icon's gating across the fixture roles (the collaborator
and the spectator never see it, and the route guard bounces their
direct ``/admin/users`` navigations), the Users tab (list + email
filter + the created-desc/email-asc sort chips, the Add User dialog,
the Edit User dialog — a handle and a new password that really logs
the user in —, an API-armed disable refusing login, and delete with
the owned-workspaces confirmation), the Groups tab (create through the
dialog, the workspace-role source chip, member add/remove through the
members dialog, delete, and the ``admins``-group membership flipped
through that same dialog — the member's admin icon follows on the next
login), the Invitations tab (create with status, resend, revoke, and
the revoked token's acceptance refused; the canonical happy-path
accept-invite scenario lives in ``test_auth.py``
#3233 — cross-referenced, not duplicated), the Events tab (the All
merged stream and the Audit and Containers subtabs showing rows for
the very mutations the earlier scenarios performed, the substring
filters narrowing them, and the expandable row detail), and the Server
tab (publish a stop and a recycle window through the dialog, cancel
both through the panel, back to empty).

Event retention/verbosity knobs (``audit_events_retention_days``,
``container_events_row_cap``, …) are deploy env vars: they are not
exposed on ``/api/v1/config`` and nothing in the panels renders them,
so there is no settings-swap surface to assert here — retention only
shows up through the hourly sweeper.

Icon-only row actions (delete user / group, resend, revoke, add /
remove member, cancel schedule) carry no semantic label — tooltips do
not surface — so they are addressed by bounds within their row's band
(``tap_row_button``). Icon-only app-bar/tab actions that DO carry
``semanticLabel`` (Admin, Logout, the Skeuo tabs) are addressed by
label.

Scenarios run in definition order and share one chain — the Events
scenario reads the rows the Users/Groups scenarios wrote, so a ``-k``
selection breaks it. A module-level sweep first clears what a
hard-killed earlier run leaks (spectator out of ``admins``, pending
server schedules — they persist across restarts and a leaked stop
would fire mid-run —, stale run-unique users and groups). USER_B is
deliberately left registered (its sweep catches it next run), matching
the sibling suites' fresh-user precedent.
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
    wait_for_fields,
)

RUN = uuid.uuid4().hex[:6]
USER_A = f"fmtk-zaa{RUN}@example.com"  # created, edited, disabled, deleted
USER_B = f"fmtk-zab{RUN}@example.com"  # the sort pair; swept next run
USER_PW = f"fmtk-Za{RUN}!a1"
USER_PW2 = f"fmtk-Zb{RUN}!b2"
HANDLE = f"e2ehandle{RUN}"
GROUP_NAME = f"e2e-grp-{RUN}"
GROUP_FILTER = f"e2e-grp-{RUN[:4]}"  # narrows to this run's group only
INVITE_EMAIL = f"fmtk-inva{RUN}@example.com"
REVOKE_EMAIL = f"fmtk-invr{RUN}@example.com"
REVOKE_PW = f"fmtk-Rv{RUN}!c3"
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


def open_admin_users(app) -> None:
    """Route to the admin page and wait for the Users tab's toolbar
    (the 'Handle' sort chip is unique to it)."""
    app.navigate("/admin/users")
    app.wait_for_text("Handle")


def admin_login(harness, app) -> None:
    """Log in as admin and land on the Users tab.  If the admin is
    already authenticated (prior test left a live session), skip the
    login round-trip — just navigate to the admin page."""
    app.navigate("/admin/users")
    if app.has_text("Handle", 5000):
        return  # already on the admin users page
    at_login(harness, app)
    # the login may land on /admin/users (the prior navigate primed the
    # route) or the workspace list — accept either destination, then
    # navigate explicitly to the admin users page
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text=ADMIN_EMAIL)
    open_admin_users(app)


def text_fields(app) -> list[dict]:
    return find_nodes(app.snapshot(), lambda n: node_type(n) == "textField")


def set_filter(app, query: str, identifier: str = "users-filter") -> None:
    """Type ``query`` into the toolbar filter addressed by its semantic
    ``identifier`` and let the 300ms debounce + reload settle.

    The admin page uses an IndexedStack — all tabs' fields coexist in
    the semantic tree. Each tab's toolbar carries a unique
    ``searchIdentifier`` (#3240) so the harness can disambiguate."""
    app.enter_text_identifier(identifier, query)
    time.sleep(1)


def row_top(app, label: str) -> float:
    """The top edge of the first node carrying ``label`` as a whole
    line — the vertical position used for row-order assertions."""
    hits = [
        node
        for node in find_label_nodes(app.snapshot(), label, exact=True)
        if node_type(node) != "textField"
    ]
    if not hits:
        raise FmtkError(f"no snapshot node labeled {label!r}")
    return (hits[0].get("bounds") or {}).get("top", -1)


def wait_row_order(app, upper: str, lower: str, timeout: float = 20) -> None:
    """Block until the ``upper`` row sits above the ``lower`` row."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if row_top(app, upper) < row_top(app, lower):
                return
        except FmtkError:
            pass  # rows not yet visible after a reload — retry
        time.sleep(1)
    raise FmtkError(f"{upper!r} never rose above {lower!r}")


def tap_row_button(app, needle: str, index: int = 0, exact: bool = True) -> None:
    """Tap the ``index``-th (left-to-right) narrow trailing button on
    the row whose labels carry ``needle``.

    The admin rows' icon actions (delete, add/remove member, resend,
    revoke, cancel schedule) are IconButtons whose tooltips never reach
    the semantic tree — they are addressed by bounds: the narrow
    tappable buttons vertically overlapping the labeled row node. A
    toolbar field carrying ``needle`` as its typed value is never the
    row (search filters are typed as prefixes, rows carry full emails),
    so field hits are skipped."""
    tree = app.snapshot()
    hits = [
        node
        for node in find_label_nodes(tree, needle, exact=exact)
        if node_type(node) != "textField"
    ]
    if not hits:
        raise FmtkError(f"no snapshot node labeled {needle!r}")
    band = hits[0].get("bounds") or {}

    def trailing(node: dict) -> bool:
        b = node.get("bounds") or {}
        width = b.get("right", 0) - b.get("left", 0)
        return (
            node_type(node) == "button"
            and "tap" in (node.get("actions") or [])
            and width <= 110
            and b.get("bottom", 0) > band.get("top", 0)
            and b.get("top", 0) < band.get("bottom", 0)
        )

    buttons = sorted(
        (n for n in find_nodes(tree, trailing) if n["ref"] != hits[0]["ref"]),
        key=lambda n: (n.get("bounds") or {}).get("left", 0),
    )
    if len(buttons) <= index:
        raise FmtkError(f"row {needle!r} has {len(buttons)} trailing buttons")
    app.tap(buttons[index]["ref"])


def tap_row_containing(app, needle: str, min_top: int = 200) -> None:
    """Tap the first tappable node below ``min_top`` carrying ``needle``
    (a table row). The app-bar email chip above carries the admin
    address too, so events-table rows are addressed with a floor."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        hits = [
            node
            for node in find_label_nodes(app.snapshot(), needle)
            if "tap" in (node.get("actions") or [])
            and (node.get("bounds") or {}).get("top", 0) >= min_top
        ]
        if hits:
            return app.tap(hits[0]["ref"])
        time.sleep(1)
    raise FmtkError(f"no tappable row below {min_top} carries {needle!r}")


# --- API-side helpers ----------------------------------------------------


def user_id_by_email(harness, email: str) -> str | None:
    status, listing = harness.admin_api("GET", f"/api/v1/users?page_size=200&q={email}")
    assert status == 200, listing
    return next((u["id"] for u in listing["users"] if u["email"] == email), None)


def group_id_by_name(harness, name: str) -> str | None:
    status, listing = harness.admin_api("GET", f"/api/v1/groups?page_size=200&q={name}")
    assert status == 200, listing
    return next((g["id"] for g in listing["groups"] if g["name"] == name), None)


def group_members(harness, group_id: str) -> list[str]:
    status, members = harness.admin_api("GET", f"/api/v1/groups/{group_id}/members")
    assert status == 200, members
    return [m["email"] for m in members]


def wait_group_member(
    harness, group_id: str, email: str, present: bool, timeout: float = 20
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (email in group_members(harness, group_id)) == present:
            return
        time.sleep(1)
    state = "in" if present else "out of"
    raise FmtkError(f"{email} never stayed {state} the group")


def remove_group_member(harness, group_id: str, user_id: str) -> None:
    """DELETE a membership; 404 (not a member) is tolerated — the
    cleanup paths run unconditionally."""
    status, _ = harness.admin_api(
        "DELETE", f"/api/v1/groups/{group_id}/members/{user_id}"
    )
    assert status in (200, 404), status


def set_user_disabled(harness, email: str, disabled: bool) -> None:
    """Arm/clear the account's disabled flag over the admin API. The
    edit dialog offers email/handle/password/must-change only —
    deactivation has no UI toggle today, so its user-visible effect
    (login refused) is asserted with the flag armed this way."""
    user_id = user_id_by_email(harness, email)
    assert user_id, f"{email} not found"
    status, body = harness.admin_api(
        "PATCH", f"/api/v1/users/{user_id}", {"disabled": disabled}
    )
    assert status == 200, body


def owner_token(harness) -> str:
    return http_login(harness.backend.url, ADMIN_EMAIL, FIXTURE_PASSWORD)


def own_workspace_id(harness, name: str) -> str:
    status, mine = http_api(
        harness.backend.url, owner_token(harness), "GET", "/api/v1/workspaces"
    )
    assert status == 200, mine
    return next(w["id"] for w in mine if w["name"] == name)


def wait_ws_running(
    harness, token: str, ws_id: str, running: bool, timeout: float = 180
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, body = http_api(
            harness.backend.url,
            token,
            "GET",
            f"/api/v1/workspaces/{ws_id}/status",
        )
        assert status == 200, body
        if bool(body.get("running")) == running:
            return
        time.sleep(2)
    raise FmtkError(f"workspace {ws_id} never reached running={running}")


def pending_schedule_actions(harness) -> set[str]:
    status, body = harness.admin_api("GET", "/api/v1/server/schedule")
    assert status == 200, body
    return {s["action"] for s in body["schedules"]}


def sweep_stale_admin_state(harness) -> None:
    """Clear what a hard-killed earlier run leaks into this suite's
    surfaces (see the module docstring)."""
    spectator = user_id_by_email(harness, SPECTATOR_EMAIL)
    admins = group_id_by_name(harness, "admins")
    if spectator and admins:
        remove_group_member(harness, admins, spectator)
    status, body = harness.admin_api("GET", "/api/v1/server/schedule")
    if status == 200:
        for schedule in body["schedules"]:
            harness.admin_api("DELETE", f"/api/v1/server/schedule/{schedule['id']}")
    status, listing = harness.admin_api("GET", "/api/v1/users?page_size=200&q=fmtk-z")
    if status == 200:
        for user in listing["users"]:
            harness.admin_api("DELETE", f"/api/v1/users/{user['id']}")
    status, listing = harness.admin_api("GET", "/api/v1/groups?page_size=200&q=e2e-grp")
    if status == 200:
        for group in listing["groups"]:
            harness.admin_api("DELETE", f"/api/v1/groups/{group['id']}")


@pytest.fixture(autouse=True, scope="module")
def swept_admin_state(harness):
    sweep_stale_admin_state(harness)
    yield


# --- scenario building blocks -------------------------------------------


def picker_result_email(app, prefix: str) -> str:
    """The type-ahead result tile's email for ``prefix`` — the tile's
    title line starts with the prefix and carries an ``@`` (the picker
    field's own value is the bare prefix)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for node in find_label_nodes(app.snapshot(), prefix):
            if node_type(node) == "textField":
                continue
            for key in ("label", "text"):
                for line in str(node.get(key) or "").split("\n"):
                    if line.startswith(prefix) and "@" in line:
                        return line
        time.sleep(1)
    raise FmtkError(f"no picker result for {prefix!r}")


def add_dialog_member(app, harness, group_id: str, email_prefix: str) -> None:
    """Type a prefix into the open members dialog's picker and tap the
    result row's add button; the membership is asserted over the API
    (the dialog's result row and member row are otherwise the same
    text, and the API is the unambiguous landing signal)."""
    app.enter_text(text_fields(app)[0]["ref"], email_prefix)
    email = picker_result_email(app, email_prefix)
    tap_row_button(app, email)
    wait_group_member(harness, group_id, email, present=True)


def invite_via_dialog(app, email: str) -> None:
    app.tap_label("Invite user")
    app.wait_for_text("Invite User")
    # the dialog's email field carries the "Email" label — pick the
    # textField labeled "Email" (unique to the dialog; the toolbar
    # fields carry different labels)

    email_field = None
    for node in find_label_nodes(app.snapshot(), "Email", exact=True):
        if node_type(node) == "textField":
            email_field = node["ref"]
            break
    assert email_field, "invite dialog email field not found"
    app.enter_text(email_field, email)
    # wait for the dialog rebuild after the text change — the button is
    # disabled until canInvite recomputes, which requires a setState
    # rebuild triggered by the text field's onChanged callback
    app.wait_for_text(email)
    app.tap_button_exact("Send Invitation")
    # the dialog closes and _inviteUser fires the POST; the confirmation
    # is the snackbar "Invitation sent to <email>", but the snackbar
    # auto-dismisses in 4s and the concurrent _loadInvitations rebuild
    # can race the SnackBar off the scaffold before wait_for catches it —
    # wait for the dialog to close instead (the caller waits for the
    # invitation row in the list)
    app.wait_gone("Invite User")


# --- scenarios ---------------------------------------------------------


def test_admin_icon_gating_and_route_guard(harness, app):
    # the admins-group member sees the icon; the admin page mounts
    at_login(harness, app)
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    app.tap_label("Admin")
    app.wait_for_text("Handle")  # the Users toolbar's sort chip
    app.logout()
    # the role members never see the icon, and the route guard bounces
    # a direct /admin navigation back to the workspaces list
    for email in (COLLAB_EMAIL, SPECTATOR_EMAIL):
        at_login(harness, app)
        app.login(email, FIXTURE_PASSWORD, expect_text="No workspaces yet")
        assert not find_label_nodes(app.snapshot(), "Admin", exact=True)
        app.navigate("/admin/users")
        app.wait_for_text("Owned by Me")
        assert not app.has_text("Handle", 2000)
        app.logout()


def test_users_list_filter_create_edit_disable_delete(harness, app):
    admin_login(harness, app)

    # --- the list renders and the email filter narrows it
    set_filter(app, "fmtk-spectator")
    app.wait_for_text(SPECTATOR_EMAIL)
    app.wait_gone(CODER_EMAIL, 10)

    # --- create two users through the Add User dialog (the filter
    # keeps the list to the run-unique rows and the FAB clear of row
    # buttons, and shows each new row the moment it lands)
    set_filter(app, "fmtk-z")
    for email in (USER_A, USER_B):
        app.tap_label("Add user")  # the Users tab FAB
        fields = wait_for_fields(app, "Add User")
        app.enter_text(fields[0]["ref"], email)
        app.enter_text(fields[1]["ref"], USER_PW)
        app.enter_text(fields[2]["ref"], USER_PW)
        app.tap_button_exact("Add")
        app.wait_gone("Add User")
        app.wait_for_text(email)

    # default sort is created-desc: B (created second) sits above A...
    wait_row_order(app, USER_B, USER_A)
    # ...and the Email sort chip flips to ascending: A above B
    app.tap(app.ref_for_label("Email", "button"))
    wait_row_order(app, USER_A, USER_B)

    # --- edit: a handle and a new password through the Edit User
    # dialog; the row shows the handle and the new password logs in
    set_filter(app, "fmtk-zaa")
    app.tap_labeled_exact(USER_A)
    fields = wait_for_fields(app, "Edit User")
    app.enter_text(fields[1]["ref"], HANDLE)
    app.enter_text(fields[2]["ref"], USER_PW2)
    fields = wait_for_fields(app, "Confirm New Password")  # confirm appeared
    app.enter_text(fields[3]["ref"], USER_PW2)
    app.tap_button_exact("Save")
    app.wait_gone("Edit User")
    app.wait_for_text(f"@{HANDLE}")
    # an admin-set password implies must_change_password — clear the flag
    # so the login assertion below reaches the workspace list, not the
    # forced-change page (the forced-change flow is covered in test_auth)
    uid = user_id_by_email(harness, USER_A)
    harness.admin_api("PATCH", f"/api/v1/users/{uid}", {"must_change_password": False})
    app.logout()

    at_login(harness, app)
    app.login(USER_A, USER_PW2, expect_text=USER_A)
    app.logout()

    # --- a disabled account is refused at login (no UI toggle — the
    # flag is armed over the API, the refusal is the UI assertion)
    set_user_disabled(harness, USER_A, disabled=True)
    at_login(harness, app)
    app.login(USER_A, USER_PW2, expect_text="Account disabled")
    set_user_disabled(harness, USER_A, disabled=False)

    # --- delete through the UI: the confirmation shows the owned-
    # workspaces fetch (none), the row leaves, the login is refused
    admin_login(harness, app)
    set_filter(app, "fmtk-zaa")
    app.scroll_until_label(USER_A)
    tap_row_button(app, USER_A)
    app.wait_for_text("They own no workspaces")
    app.tap_button_exact("Delete")
    app.wait_gone(USER_A)
    app.logout()

    at_login(harness, app)
    app.login(USER_A, USER_PW2, expect_text="Invalid credentials")


def test_groups_create_members_delete_and_admin_icon(harness, app):
    admin_login(harness, app)
    app.tap_labeled_exact("Groups")
    app.wait_for_text("Workspace role groups")
    app.wait_for_text("admins")  # the seeded manual group

    # --- create through the dialog; the filter shows the row land
    set_filter(app, GROUP_FILTER, identifier="groups-filter")
    app.tap_label("Create group")  # the Groups tab FAB
    fields = wait_for_fields(app, "Create Group")
    app.enter_text(fields[0]["ref"], GROUP_NAME)
    app.enter_text(fields[1]["ref"], "e2e run-unique group")
    app.tap_button_exact("Create")
    app.wait_gone("Create Group")
    app.wait_for_text(GROUP_NAME)
    group_id = group_id_by_name(harness, GROUP_NAME)
    assert group_id

    # --- members: add through the type-ahead, remove through the row
    app.tap_labeled_exact(GROUP_NAME)
    app.wait_for_text("Members of")
    add_dialog_member(app, harness, group_id, CODER_EMAIL)
    tap_row_button(app, CODER_EMAIL)  # the member row's remove button
    wait_group_member(harness, group_id, CODER_EMAIL, present=False)
    app.tap_button_exact("Done")
    app.wait_gone("Members of")

    # --- delete through the row's button + confirmation
    tap_row_button(app, GROUP_NAME)
    app.wait_for_text("Delete Group")
    app.tap_button_exact("Delete")
    app.wait_gone(GROUP_NAME)

    # --- the source chip: manual-only hides the seeded workspace role
    # groups, the chip includes them
    set_filter(app, "", identifier="groups-filter")
    app.wait_for_text("admins")
    assert not find_label_nodes(app.snapshot(), "collaborators-")
    app.tap_labeled_exact("Workspace role groups")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if find_label_nodes(app.snapshot(), "collaborators-"):
            break
        time.sleep(1)
    else:
        raise FmtkError("the workspace-role chip never surfaced role groups")
    app.tap_labeled_exact("Workspace role groups")

    # --- admins membership flipped through the same dialog: the
    # member's admin icon follows on the next login
    admins = group_id_by_name(harness, "admins")
    assert admins
    try:
        app.tap_labeled_exact("admins")
        app.wait_for_text("Members of")
        add_dialog_member(app, harness, admins, SPECTATOR_EMAIL)
        app.tap_button_exact("Done")
        app.wait_gone("Members of")
        app.logout()
        at_login(harness, app)
        app.login(SPECTATOR_EMAIL, FIXTURE_PASSWORD, expect_text=SPECTATOR_EMAIL)
        # the spectator is now an admins-group member — the icon appears
        app.navigate("/workspaces")
        app.wait_for_label("Admin")
        app.logout()
        # removal through the same dialog: the icon follows off
        admin_login(harness, app)
        app.tap_labeled_exact("Groups")
        app.wait_for_text("Workspace role groups")
        app.tap_labeled_exact("admins")
        app.wait_for_text("Members of")
        tap_row_button(app, SPECTATOR_EMAIL)
        wait_group_member(harness, admins, SPECTATOR_EMAIL, present=False)
        app.tap_button_exact("Done")
        app.wait_gone("Members of")
    finally:
        # a hard failure mid-flow must not leave the fixture spectator
        # an admin (the gating scenario and the sibling suites' role
        # matrix depend on it)
        spectator = user_id_by_email(harness, SPECTATOR_EMAIL)
        if spectator and SPECTATOR_EMAIL in group_members(harness, admins):
            remove_group_member(harness, admins, spectator)
    app.logout()
    at_login(harness, app)
    app.login(SPECTATOR_EMAIL, FIXTURE_PASSWORD, expect_text=SPECTATOR_EMAIL)
    app.navigate("/workspaces")
    app.wait_for_text("No workspaces")
    assert not find_label_nodes(app.snapshot(), "Admin", exact=True)
    app.logout()


def test_invitations_status_resend_revoke(harness, app):
    admin_login(harness, app)
    app.tap_labeled_exact("Invitations")
    app.wait_for_text("Invited by")  # the toolbar's column header

    # --- create: the row appears with its status and inviter
    set_filter(app, f"fmtk-inva{RUN}", identifier="invitations-filter")
    invite_via_dialog(app, INVITE_EMAIL)
    app.wait_for_text(INVITE_EMAIL)
    app.wait_for_text("Status: pending")
    # --- resend: the button fires a POST; verify the email arrived in
    # the SMTP sink rather than the transient snackbar (same race as the
    # initial invite — the SnackBar auto-dismisses under load)
    tap_row_button(app, INVITE_EMAIL, index=0)
    harness.smtp.token_for("accept-invite", INVITE_EMAIL)

    # a second invitation, then revoked: its emailed token must stop
    # working (the canonical happy-path acceptance lives in the auth
    # suite, #3233 — this is the revocation half)
    set_filter(app, f"fmtk-invr{RUN}", identifier="invitations-filter")
    invite_via_dialog(app, REVOKE_EMAIL)
    app.wait_for_text(REVOKE_EMAIL)
    token = harness.smtp.token_for("accept-invite", REVOKE_EMAIL)
    tap_row_button(app, REVOKE_EMAIL, index=1)  # revoke (right of resend)
    app.wait_for_text("Revoke Invitation")
    app.tap_button_exact("Revoke")
    # the row stays with status changed to "revoked" (not deleted)
    app.wait_for_text("Status: revoked")
    app.logout()

    # the revoked token's acceptance is refused on the accept page
    at_login(harness, app)
    app.navigate(f"/accept-invite?token={token}")
    app.wait_for_text("Confirm Password")
    fields = wait_for_fields(app, "Confirm Password")
    app.enter_text(fields[0]["ref"], REVOKE_PW)
    app.enter_text(fields[1]["ref"], REVOKE_PW)
    app.tap_button_exact("Create Account")
    app.wait_for_text("no longer valid")


def test_events_tabs_record_the_runs_mutations(harness, app):
    # the Containers subtab's content: this run's own transitions on
    # the fixture workspace (stop-first makes the sequence start clean
    # whatever state an earlier suite left)
    ws_id = own_workspace_id(harness, "fmtk-verify")
    token = owner_token(harness)
    for action, running in (("stop", False), ("start", True), ("stop", False)):
        status, body = http_api(
            harness.backend.url,
            token,
            "POST",
            f"/api/v1/workspaces/{ws_id}/{action}",
        )
        assert status == 200, body
        wait_ws_running(harness, token, ws_id, running)

    admin_login(harness, app)
    app.tap_labeled_exact("Events")
    # the All subtab (default) loads the merged stream on mount;
    # rows render as "time\nsource\nevent\nactor[\nworkspace]" —
    # the row labels carry bare event words ("login", "start", "stop")
    # and the source as a separate cell ("audit", "container").
    # Login events always exist from the admin login that just happened.
    app.wait_for_text("audit", 30000)  # at least one audit row loaded

    # the Containers subtab: this run's start/stop transitions
    app.tap_labeled_exact("Containers")
    app.wait_for_text("fmtk-verify", 30000)
    assert app.has_text("start", 5000)
    assert app.has_text("stop", 5000)

    # the Audit subtab: login events always exist; verify an expandable
    # row detail (the expand/collapse is the Audit subtab's own surface)
    app.tap_labeled_exact("Audit")
    app.wait_for_text(ADMIN_EMAIL, 30000)
    tap_row_containing(app, ADMIN_EMAIL)
    app.wait_for_text("Detail")


def test_server_schedule_publish_and_cancel(harness, app):
    admin_login(harness, app)
    app.tap_labeled_exact("Server")
    app.wait_for_text("No scheduled server actions")

    # publish a stop window (the long delay keeps it from firing
    # mid-run; the panel is drained again at the end)
    app.tap_label("Schedule server action")  # the Server tab FAB
    app.wait_for_text("Schedule Server Action")
    app.enter_text(app.ref_for_label("Delay", "textField"), "119")
    app.wait_for_text("Fires")  # the dialog's live preview
    app.tap_button_exact("Schedule")
    app.wait_gone("Schedule Server Action")
    app.wait_for_text("Stop at")
    assert pending_schedule_actions(harness) == {"stop"}

    # publish a recycle window too: two pending cards
    app.tap_label("Schedule server action")
    app.wait_for_text("Schedule Server Action")
    app.tap_labeled_exact("Recycle")
    app.wait_for_text("graceful in-place restart")
    app.enter_text(app.ref_for_label("Delay", "textField"), "90")
    app.tap_button_exact("Schedule")
    app.wait_gone("Schedule Server Action")
    app.wait_for_text("Recycle at")
    assert pending_schedule_actions(harness) == {"stop", "recycle"}

    # cancel both through the panel's own affordance; back to empty
    for card, title in (
        ("Stop at", "Cancel Scheduled stop"),
        ("Recycle at", "Cancel Scheduled recycle"),
    ):
        tap_row_button(app, card, exact=False)
        app.wait_for_text(title)
        app.tap_button_exact("Cancel Schedule")
        app.wait_gone(card)
    app.wait_for_text("No scheduled server actions")
    assert pending_schedule_actions(harness) == set()
    app.logout()
