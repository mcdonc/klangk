"""fmtk e2e: auth and session flows (#3233).

Every user-visible auth/session surface, driven through the real UI:
login (success + wrong password), logout + deep-link bounce, pending
redirect honored post-login, registration + email verification +
resend, forgot/reset password, forced password change, step-up (sudo)
dialog on an admin invite, invitation acceptance, silent token refresh,
session invalidation landing on login, and the SIGHUP-swapped
registration/password-policy config knobs.

Email-token flows use the harness SMTP sink: klangkd delivers the
verify/reset/invite links to the in-process server, the tests extract
the token, and ``navigate()`` hash-routes the driven tab to the link's
page (the same thing a user clicking the email link does).

Scenarios run in definition order and each ends logged out on the login
page, so any single scenario can be re-run alone with ``-k``.
"""

from __future__ import annotations

import time
import uuid

from fmtkharness import (
    ADMIN_EMAIL,
    FIXTURE_PASSWORD,
    FmtkError,
    find_nodes,
    node_type,
)

RUN = uuid.uuid4().hex[:6]
MUSTCHANGE_EMAIL = "fmtk-mustchange@example.com"
MUSTCHANGE_PW = f"fmtk-Mc{RUN}!A1"
MUSTCHANGE_NEW = f"fmtk-Post{RUN}!C3"
RESET_EMAIL = "fmtk-reset@example.com"
RESET_NEW = f"fmtk-Rs{RUN}!B2"
REGISTERED_EMAIL = f"fmtk-new{RUN}@example.com"
REGISTERED_PW = "fmtk-Reg123!"
INVITED_EMAIL = f"fmtk-inv{RUN}@example.com"
INVITED_PW = f"fmtk-Ac{RUN}!D4"


def walk_fields(app) -> list[dict]:
    """The visible text fields, in reading order."""
    return find_nodes(app.snapshot(), lambda n: node_type(n) == "textField")


def at_login(harness, app) -> None:
    """Land on the usable login form: route there, wait for the page,
    and dismiss any leftover login-banner consent dialog (a swapped
    banner's dialog masks the form from the semantic tree)."""
    app.navigate("/login")
    if not app.has_text("Log In", 10000):
        # a leftover session guards /login away (a must-change user is
        # bounced back to /change-password; a normal one to
        # /workspaces) — end it, then the route sticks. A dead app
        # (closed window, lost isolate) restarts instead.
        try:
            app.auth_eval("auth!.logout(); return 'ok';")
        except FmtkError:
            harness.restart_app()
    app.wait_for_login_page()
    app.dismiss_login_banner()
    app.wait_for_text("Email or handle")


def test_wrong_password_shows_error_not_navigation(harness, app):
    at_login(harness, app)
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], ADMIN_EMAIL)
    app.enter_text(fields[1]["ref"], "definitely-wrong")
    app.tap_label("Log In")
    app.wait_for_text("Invalid credentials")
    assert not app.has_text("Owned by Me", 2000)  # stayed on login
    # correct credentials now land in the app
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
    app.logout()


def test_register_verify_and_resend(harness, app):
    at_login(harness, app)
    app.tap_label("Need an account? Create one")
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], REGISTERED_EMAIL)
    app.enter_text(fields[1]["ref"], REGISTERED_PW)
    app.tap_label("Create Account")
    app.wait_for_text("Check your email to verify your account.")
    # login before verifying: refused with the resend affordance
    app.tap_label("Already have an account? Log in")
    app.login(REGISTERED_EMAIL, REGISTERED_PW, expect_text="Account not verified")
    app.tap_label("Resend verification email")
    app.wait_for_text("Verification email sent. Check your inbox.")
    token = harness.smtp.token_for("verify", REGISTERED_EMAIL)
    app.navigate(f"/verify?token={token}")
    # auto-login lands on /workspaces; the empty owned tab copy is the
    # matchable text for a workspace-less user
    app.wait_for_text("No workspaces yet. Create one to get started.")
    app.logout()


def test_forgot_and_reset_password(harness, app):
    at_login(harness, app)
    app.tap_label("Forgot password?")
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], RESET_EMAIL)
    app.tap_label("Send Reset Link")
    app.wait_for_text("we sent a password reset link")
    token = harness.smtp.token_for("reset-password", RESET_EMAIL)
    app.navigate(f"/reset-password?token={token}")
    app.wait_for_text("New Password")
    # weak new password: rejected inline by the client-side policy mirror
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], "short")
    app.enter_text(fields[1]["ref"], "short")
    app.tap_label("Reset Password")
    app.wait_for_text("Min 8 characters")
    # a good one completes the reset and logs straight in
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], RESET_NEW)
    app.enter_text(fields[1]["ref"], RESET_NEW)
    app.tap_label("Reset Password")
    app.wait_for_text("No workspaces yet. Create one to get started.")
    app.logout()


def test_forced_password_change(harness, app):
    harness.force_password_change(MUSTCHANGE_EMAIL, MUSTCHANGE_PW)
    at_login(harness, app)
    app.login(MUSTCHANGE_EMAIL, MUSTCHANGE_PW, expect_text="Password Change Required")
    # weak new password: rejected inline
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], MUSTCHANGE_PW)
    app.enter_text(fields[1]["ref"], "short")
    app.enter_text(fields[2]["ref"], "short")
    app.tap_label("Change Password")
    app.wait_for_text("Min 8 characters")
    # a good one ends the session and sends the user to log in afresh
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], MUSTCHANGE_PW)
    app.enter_text(fields[1]["ref"], MUSTCHANGE_NEW)
    app.enter_text(fields[2]["ref"], MUSTCHANGE_NEW)
    app.tap_label("Change Password")
    app.wait_for_text("Log in with your new password")
    app.wait_for_login_page()
    # Re-login, then navigate explicitly: the address-bar hash can lag
    # the change-password -> /login transition, and a router re-parse
    # of the stale #/change-password re-mounts the change page for the
    # now-healthy session (guardForcedPasswordChange allows it) — see
    # the routing race noted in the PR. Navigating past it makes the
    # destination deterministic.
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], MUSTCHANGE_EMAIL)
    app.enter_text(fields[1]["ref"], MUSTCHANGE_NEW)
    app.tap_label("Log In")
    time.sleep(2)  # let the login land before routing past the stale hash
    app.navigate("/workspaces")
    app.wait_for_text("No workspaces yet. Create one to get started.")
    app.logout()


def test_step_up_dialog_and_invite_acceptance(harness, app):
    original = harness.config.get("step_up_window_minutes", "0")
    # step_up_window_minutes is not surfaced on /api/v1/config — the
    # dialog itself is the behavioral verification
    harness.backend.swap_settings(
        {"step_up_window_minutes": "60"}, apply="sighup", verify=False
    )
    try:
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
        app.tap_label("Admin")  # icon has a semanticLabel now
        app.wait_for_text("fmtk-admin@example.com")
        # the invite FAB lives on the Invitations tab (the Users tab's
        # corner FAB is add-user — same dialog copy, wrong flow)
        app.tap_label("Invitations")
        app.wait_for_text("Email")  # the invitations toolbar column header
        app.tap_lowest_button()  # the FAB: tap-action-filtered corner button
        app.wait_for_text("An email will be sent")
        fields = walk_fields(app)
        app.enter_text(fields[0]["ref"], INVITED_EMAIL)
        app.tap_label("Send Invitation")
        # the write is refused with step_up_required -> the sudo dialog
        app.wait_for_text("Re-authentication required")
        # wrong password re-prompts without retrying the write. The
        # dialog's field is selected BY LABEL: the admin page behind it
        # carries its own filter textFields, so positional field picking
        # types into the wrong one.
        app.enter_text(app.ref_for_label("Password", "textField"), "definitely-wrong")
        app.tap_label("Confirm")
        app.wait_for_text("Incorrect password — try again.")
        # correct password confirms and the retried invite lands
        app.enter_text(app.ref_for_label("Password", "textField"), FIXTURE_PASSWORD)
        app.tap_label("Confirm")
        app.wait_gone("Re-authentication required")
        app.wait_for_text(f"Invitation sent to {INVITED_EMAIL}")
        app.logout()
    finally:
        harness.backend.swap_settings(
            {"step_up_window_minutes": original}, apply="sighup", verify=False
        )
    # the invited user accepts over the emailed link and lands in the app
    token = harness.smtp.token_for("accept-invite", INVITED_EMAIL)
    app.navigate(f"/accept-invite?token={token}")
    app.wait_for_text("Confirm Password")
    fields = walk_fields(app)
    app.enter_text(fields[0]["ref"], INVITED_PW)
    app.enter_text(fields[1]["ref"], INVITED_PW)
    app.tap_label("Create Account")
    app.wait_for_text("No workspaces yet. Create one to get started.")
    app.logout()


def test_silent_token_refresh_keeps_session(harness, app):
    original_ttl = harness.config.get("access_token_hours", "24")
    harness.backend.swap_settings(
        {"access_token_hours": "0.0008"}, apply="sighup", verify=False
    )
    try:
        # ~1s tokens: the client's 80% refresh timer must silently renew
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
        time.sleep(4)  # outlives several token lifetimes
        assert (
            "true"
            in str(app.auth_eval("final v = auth!.isLoggedIn; return v;")).lower()
        )
        app.wait_for_text("fmtk-verify")  # never bounced to login
        app.logout()
    finally:
        harness.backend.swap_settings(
            {"access_token_hours": original_ttl}, apply="sighup", verify=False
        )


def test_invalidated_session_lands_on_login(harness, app):
    original_secret = harness.config["jwt_secret"]
    try:
        at_login(harness, app)
        app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="fmtk-verify")
        # rotating the signing secret invalidates the session the app
        # holds; the next scheduled refresh gets 401 -> the client clears
        # its token and the router lands on login (not a blank screen)
        harness.backend.swap_settings(
            {"jwt_secret": f"fmtk-rotated-{RUN}"}, apply="sighup", verify=False
        )
        # force the client's refresh cycle now (the scheduled one is
        # hours out): 401 -> the client clears its token and the router
        # lands on login (not a blank screen)
        app.auth_eval("auth!.testRefreshToken(); return 'ok';")
        app.wait_for_login_page(timeout_ms=30000)
        assert not app.has_text("fmtk-verify", 2000)
    finally:
        harness.backend.swap_settings(
            {"jwt_secret": original_secret},
            apply="sighup",
            verify=False,
        )


def test_registration_toggle_and_policy_swap(harness, app):
    original_reg = harness.config.get("disable_registration", "")
    original_min = harness.config.get("min_password_length", "8")
    try:
        harness.backend.swap_settings(
            {"disable_registration": "true"},
            apply="sighup",
            verify=False,
        )
        harness.backend.wait_config_value("registration_enabled", False)
        # remount the login page (a same-location hash change does not
        # rebuild it) so the fresh /config fetch reflects the swap
        app.navigate("/forgot-password")
        app.wait_for_text("Send Reset Link")
        app.navigate("/login")
        app.wait_for_login_page()
        app.dismiss_login_banner()
        app.wait_gone("Need an account? Create one")
        harness.backend.swap_settings(
            {
                "disable_registration": original_reg,
                "min_password_length": "14",
            },
            apply="sighup",
            verify=False,
        )
        harness.backend.wait_config_value("min_password_length", 14)
        # the register form's validator reads AuthService's policy
        # snapshot, not the page's own config fetch — refresh it
        app.auth_eval("auth!.refreshDeployConfig(); return 'ok';")
        time.sleep(2)
        # the login page re-fetches /config on every mount — bounce to
        # another public page and back instead of restarting the app
        app.navigate("/forgot-password")
        app.wait_for_text("Send Reset Link")
        app.navigate("/login")
        app.wait_for_login_page()
        app.dismiss_login_banner()
        app.tap_label("Need an account? Create one")
        fields = walk_fields(app)
        app.enter_text(fields[0]["ref"], f"fmtk-len{RUN}@example.com")
        app.enter_text(fields[1]["ref"], "tenchars!!")
        app.tap_label("Create Account")
        app.wait_for_text("Min 14 characters")  # live policy mirror
    finally:
        harness.backend.swap_settings(
            {
                "disable_registration": original_reg,
                "min_password_length": original_min,
            },
            apply="sighup",
            verify=False,
        )
        harness.backend.wait_config_value("registration_enabled", True)
        app.navigate("/forgot-password")
        app.wait_for_text("Send Reset Link")
        app.navigate("/login")
        app.wait_for_login_page()
        app.dismiss_login_banner()
        assert app.has_text("Need an account? Create one", 5000)


def test_logout_bounces_deep_links_to_login(harness, app):
    # runs LAST by file order: booting the app at a deep link leaves an
    # instance that dies shortly after (initial-route fallback fallout,
    # see the PR's routing-race note), so nothing may follow it here.
    # logged out by the previous scenario; booting the app AT a protected
    # deep link (the real pre-auth email/deep-link UX) must bounce to
    # login with the pending-redirect message (guardAuth at boot). A
    # full-page navigation inside the running tab is not an option — it
    # kills the dwds isolate — so this boots a fresh app at the URL.
    harness.restart_app(at_path="/settings")
    app.wait_for_login_page()
    app.wait_for_text("Please log in to continue.")
    assert not app.has_text("Change Password", 2000)


def test_pending_redirect_honored_after_login(harness, app):
    # the stashed /settings target from the bounce above is honored
    app.login(ADMIN_EMAIL, FIXTURE_PASSWORD, expect_text="Settings")
    app.logout()
