"""fmtk e2e: OIDC/SSO login across enabled/disabled configurations (#3242).

Every SSO leg is driven through the real surfaces: the login page's
provider entry points (absent in the default password-only scratch
config, present once ``auth_modes: both`` + a provider ride a config
restart), the full redirect chain (button → backend authorize redirect
→ the IdP's decision page → callback → ``/#/oidc-complete`` → the
app landed and logged in), and the deny path (the IdP refuses, the
backend answers 400, the tab comes back to a login page with no
session and no provisioned user).

The IdP is the harness's in-process ``FakeIdP`` (a real HTTP server on
an ephemeral port serving discovery, JWKS, an HTML decision page, a
PKCE-checking token endpoint minting RS256 id_tokens, and
RP-initiated end-session). The Flutter app only sees the return leg,
so the cross-origin IdP legs are driven through the driven Chrome
tab's CDP (navigate/evaluate; the tab's URL is the state signal).
Same-tab full-page navigation is what a real SSO click does — the
fmtk VM service re-attaches when the chain lands back on the app
origin, and the one-time login code is redeemed by the app itself.

Identities on the decision page (stable order): the run-unique member
(JIT-provisioned on first approve), a pre-registered password user
(an SSO login for her email LINKS the identity to the existing
account — the password still works afterwards), and an
``email_verified: false`` identity (the backend refuses the claim with
403 — an IdP that has not verified the email must not mint a
session). Provider config swaps cover the post-logout pair: with
``logout-redirect: false`` the app's logout stays in-app, with ``true``
the tab is routed through the IdP's end-session endpoint and back to
``/#/login``.

No auto-provisioning toggle exists server-side — JIT creation on
first approve is the only provisioning path (the operator-side deny
knob is the login hook, ``KLANGKD_OIDC_LOGIN_HOOK``); the suite pins
the actual behavior instead. Scenarios run in definition order and
share one chain (one provider, one config-enabled module state) —
re-run the whole file.
"""

from __future__ import annotations

import time
import uuid

import pytest

from fmtkharness import (
    FakeIdP,
    cdp_eval,
    cdp_wait_tab_url,
    http_api,
    http_get_json,
)

RUN = uuid.uuid4().hex[:6]
PROVIDER_LABEL = "Log in with Fmtk SSO"
IDP_EMAIL = f"fmtk-oidc{RUN}@example.com"
UNVERIFIED_EMAIL = f"fmtk-unver{RUN}@example.com"
LINK_EMAIL = f"fmtk-lnk{RUN}@example.com"
LINK_PASSWORD = f"fmtk-Lnk{RUN}!A7"


# --- shared driving helpers (suite-local; harness keeps primitives) ----


def app_origin() -> str:
    from fmtkharness import PROXY_PORT

    return f"http://127.0.0.1:{PROXY_PORT}"


def at_login(harness, app) -> None:
    """Land on the usable login form. A parked-away tab (a failed SSO
    leg leaves it at the IdP or a backend error page) is brought back
    via CDP first — the page load re-attaches the VM service."""
    try:
        app.has_text("Log In", 5000)
    except Exception:  # noqa: BLE001 — the isolate is gone; tab is away
        cdp_eval(f"location.href='{app_origin()}/#/login'")
        wait_for_app(app, "Email or handle")
    if not app.has_text("Log In", 10000):
        app.logout()
    app.wait_for_login_page()
    app.dismiss_login_banner()
    app.wait_for_text("Email or handle")


def wait_for_app(app, expect: str, timeout: float = 90) -> None:
    """Wait until fmtk can drive the app again after a page load (the
    dwds re-attach races the boot)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if app.has_text(expect, 5000):
                return
        except Exception:  # noqa: BLE001 — dwds re-attach races
            pass
        time.sleep(2)
    raise AssertionError(f"the app never showed {expect!r}")


def remount_login(app) -> None:
    """Force a real unmount+mount of the login page so its mount-time
    config fetch runs — a redirect-only round-trip reuses the mounted
    page (stale config, no SSO button), so detour through the verify
    route first."""
    app.navigate("/verify?token=abc")
    app.wait_for_text("Email Verification", 30000)
    app.navigate("/login")
    app.wait_for_text("Email or handle")


def tap_sso(app, idp) -> None:
    """Tap the SSO button. A dispatched tap can die mid-envelope — the
    full-page navigation unloads the app before fmtk hears back — so
    the tab's URL is the source of truth: the tap worked iff the tab
    reached the IdP. Retry only while the tab is still on the app."""
    from fmtkharness import cdp_app_tab

    for _ in range(4):
        try:
            app.tap_button_exact(PROVIDER_LABEL)
        except Exception:  # noqa: BLE001 — check the tab before retrying
            pass
        url = cdp_app_tab()["url"]
        if url.startswith(idp.issuer):
            return
        time.sleep(3)
    raise AssertionError("the SSO tap never left the app origin")


def approve_at_idp(idp, app, index: int) -> None:
    """Drive the IdP decision page in the real tab: wait for the
    authorize page, prove the chain reached it with the backend's
    parameters, click an approve/deny affordance."""
    url = cdp_wait_tab_url((idp.issuer,), timeout=60)
    assert "client_id=klangk-fmtk" in url, url
    assert "code_challenge_method=S256" in url, url
    assert f"{app_origin()}/api/v1/auth/oidc/fmtk-idp/callback" in url.replace(
        "%3A", ":"
    ).replace("%2F", "/"), url
    cdp_eval(f"document.getElementById('approve-{index}').click()")


def wait_landed(app, expect: str) -> None:
    """Wait for the chain to land back on the app origin and the app
    (a fresh page load) to show ``expect`` — the VM service re-attaches
    when the tab returns, so this can take a while after the reload."""
    cdp_wait_tab_url((f"{app_origin()}/#",), timeout=90)
    wait_for_app(app, expect, timeout=90)


def return_tab_to_login(app) -> None:
    """The chain left the tab on a backend error page (its JSON body is
    the assertion surface); navigate the tab back to the app and wait
    for the login form (a fresh page load re-attaches the VM service)."""
    cdp_eval(f"location.href='{app_origin()}/#/login'")
    wait_for_app(app, "Email or handle")


def hard_reload_login(app) -> None:
    """Force a full page load of the app at /#/login.

    A hash-only navigation from a running app is same-document — the
    page keeps its boot-time config fetch, so a swapped-in banner (or a
    swapped-out one) would never be seen. The about:blank detour makes
    the return a real document load.
    """
    cdp_eval("location.href='about:blank'")
    cdp_eval(f"location.href='{app_origin()}/#/login'")


def wait_settled(app, timeout: float = 90) -> None:
    """Wait for a settled app surface: the login form or the workspace
    list (either may come out of a post-restore reload, depending on
    whether a session survived the scenario)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if app.has_text("Log In", 3000) or app.has_text("Owned by Me", 3000):
                return
        except Exception:  # noqa: BLE001 — dwds re-attach races
            pass
        time.sleep(2)
    raise AssertionError("the app settled on neither login form nor workspace list")


def body_text() -> str:
    return str(cdp_eval("document.body.innerText"))


def register_password_user(harness, email: str, password: str) -> None:
    """Register + verify a password user over HTTP (the linking
    scenario's pre-existing account)."""
    base = harness.backend.url
    status, body = http_api(
        base,
        None,
        "POST",
        "/api/v1/auth/register",
        {"email": email, "password": password},
    )
    assert status == 200 and body.get("status") == "pending_verification", body
    token = harness.smtp.token_for("verify", email)
    status, body = http_api(base, None, "POST", "/api/v1/auth/verify", {"token": token})
    assert status == 200 and "access_token" in body, body


def admin_user_entry(harness, email: str) -> dict | None:
    status, listing = harness.admin_api(
        "GET", f"/api/v1/users?search={email}&page_size=10"
    )
    assert status == 200, listing
    users = listing.get("users", [])
    return next((u for u in users if u["email"] == email), None)


# --- the OIDC stack: one IdP, one enabled config, restored on exit ----


@pytest.fixture(autouse=True, scope="module")
def oidc_stack(harness):
    """Boot the fake IdP and leave the scratch config password-only
    until a scenario enables SSO; whatever the scenarios swap, the
    original keys are restored (a kept backend must not serve SSO to
    the next suite's runs)."""
    idp = FakeIdP(
        [
            {"email": IDP_EMAIL, "sub": f"oidc-{RUN}-1", "email_verified": True},
            {"email": LINK_EMAIL, "sub": f"oidc-{RUN}-2", "email_verified": True},
            {
                "email": UNVERIFIED_EMAIL,
                "sub": f"oidc-{RUN}-3",
                "email_verified": False,
            },
        ]
    )
    idp.start()
    original = {
        "auth_modes": harness.config.get("auth_modes", "password"),
        "oidc_providers": harness.config.get("oidc_providers"),
    }
    try:
        yield idp
    finally:
        idp.stop()
        harness.backend.swap_settings(
            {
                "auth_modes": original["auth_modes"],
                "oidc_providers": original["oidc_providers"],
            },
            apply="restart",
            verify=False,
        )


def enable_sso(harness, idp, logout_redirect: bool = False) -> None:
    """Restart the backend with SSO on (``both`` keeps the password
    form beside the provider button)."""
    harness.backend.swap_settings(
        {
            "auth_modes": "both",
            "oidc_providers": [idp.provider_entry(logout_redirect=logout_redirect)],
        },
        apply="restart",
        verify=False,
    )
    harness.backend.wait_config_value("auth_modes", "both")


# --- scenarios ---------------------------------------------------------


def test_sso_entry_appears_only_when_configured(harness, app, oidc_stack):
    # disabled (the scratch default): password form, no SSO entry
    at_login(harness, app)
    assert app.has_text("Log In")
    assert not app.has_text("Log in with", 3000), "SSO button before config"

    # enabled (config restart): the provider button joins the form and
    # the public config carries the provider
    enable_sso(harness, oidc_stack)
    remount_login(app)
    app.wait_for_text(PROVIDER_LABEL, 30000)
    assert app.has_text("Log In"), "password form must stay under 'both'"
    payload = http_get_json(f"{harness.backend.url}/api/v1/config")
    assert payload["auth_modes"] == "both", payload["auth_modes"]
    assert payload["oidc_providers"] == [
        {"id": "fmtk-idp", "display_name": "Fmtk SSO"}
    ], payload["oidc_providers"]


def test_sso_approve_provisions_and_lands(harness, app, oidc_stack):
    at_login(harness, app)
    tap_sso(app, oidc_stack)
    approve_at_idp(oidc_stack, app, 0)

    # the app landed as the JIT-provisioned user (empty owned list)
    wait_landed(app, "No workspaces yet")
    assert app.has_text(IDP_EMAIL, 10000), "the provisioned user's email chip"
    app.logout()

    # server side: the user exists, OIDC-shaped (no password hash, and
    # the provider recorded)
    entry = admin_user_entry(harness, IDP_EMAIL)
    assert entry is not None, "JIT provisioning never created the user"
    assert entry.get("provider") == "fmtk-idp", entry
    # the IdP saw the whole leg: authorize -> approve -> PKCE-checked token
    assert ("token", IDP_EMAIL) in oidc_stack.events, oidc_stack.events


def test_sso_completes_under_every_visit_banner(harness, app, oidc_stack):
    """#3371: an every-visit banner must not turn SSO into a loop.

    The callback lands on a fresh app load at #/oidc-complete?code=...
    with the banner required again; the one-time code must be redeemed
    despite that (the banner gate exempts the route while logged out),
    the banner accepted on landing, and the login then completes —
    instead of the pre-fix loop where the guard dropped the code and
    bounced back to /consent, then /login, then the IdP again.
    """
    original = {
        key: harness.config.get(key, default)
        for key, default in (
            ("login_banner", ""),
            ("login_banner_title", ""),
            ("login_banner_every_visit", "false"),
        )
    }
    try:
        harness.backend.swap_settings(
            {
                "auth_modes": "both",
                "oidc_providers": [oidc_stack.provider_entry()],
                "login_banner": "fmtk every-visit banner text",
                "login_banner_title": "Fmtk Notice",
                "login_banner_every_visit": "true",
            },
            apply="restart",
            verify=False,
        )
        harness.backend.wait_config_value("auth_modes", "both")
        harness.backend.wait_config_value("login_banner_every_visit", True)

        # fresh load: the banner gate forces /consent before the login
        # form; accept, then the SSO button is on the re-mounted form
        hard_reload_login(app)
        wait_for_app(app, "I Accept")
        app.tap_label("I Accept")
        wait_for_app(app, PROVIDER_LABEL)

        tap_sso(app, oidc_stack)
        approve_at_idp(oidc_stack, app, 0)

        # the callback reloads the app with the banner pending again —
        # the code is redeemed anyway and the guards land on /consent
        # (not /login): the session exists as soon as it is accepted
        wait_landed(app, "I Accept")
        app.tap_label("I Accept")
        app.wait_for_text("No workspaces yet")
        assert app.has_text(IDP_EMAIL, 10000), "the SSO user's email chip"
        app.logout()
    finally:
        harness.backend.swap_settings(original, apply="restart", verify=False)
        # leave the tab on a clean surface for the next scenario — the
        # running app's in-memory config is stale until a real reload
        hard_reload_login(app)
        wait_settled(app)


def test_sso_deny_keeps_login_page_and_no_user(harness, app, oidc_stack):
    status, listing = harness.admin_api("GET", "/api/v1/users?page_size=1")
    users_before = listing["total"]
    at_login(harness, app)
    tap_sso(app, oidc_stack)
    url = cdp_wait_tab_url((oidc_stack.issuer,), timeout=60)
    assert "code_challenge=" in url, url
    cdp_eval("document.getElementById('deny').click()")

    # the backend answers the error callback with a plain 400 page (the
    # browser-visible denial — the app never sees a session code)
    deadline = time.monotonic() + 60
    text = ""
    while time.monotonic() < deadline:
        text = body_text()
        if "Login failed" in text:
            break
        time.sleep(1)
    assert "Login failed" in text, text
    return_tab_to_login(app)

    # nothing was provisioned, and no token was ever minted
    status, listing = harness.admin_api("GET", "/api/v1/users?page_size=1")
    assert listing["total"] == users_before, "a denied SSO login must not provision"
    assert ("deny",) in oidc_stack.events, oidc_stack.events


def test_sso_links_existing_password_account(harness, app, oidc_stack):
    register_password_user(harness, LINK_EMAIL, LINK_PASSWORD)
    at_login(harness, app)
    tap_sso(app, oidc_stack)
    approve_at_idp(oidc_stack, app, 1)

    # landed as the EXISTING account (one user, both login methods)
    wait_landed(app, "No workspaces yet")
    assert app.has_text(LINK_EMAIL, 10000)
    app.logout()

    # the password still logs the linked account in
    app.login(LINK_EMAIL, LINK_PASSWORD, expect_text="No workspaces yet")
    app.logout()


def test_unverified_email_claim_denied(harness, app, oidc_stack):
    at_login(harness, app)
    tap_sso(app, oidc_stack)
    approve_at_idp(oidc_stack, app, 2)

    # the backend refuses the claim: an IdP that did not verify the
    # email must not mint a session (403, visible in the tab)
    deadline = time.monotonic() + 60
    text = ""
    while time.monotonic() < deadline:
        text = body_text()
        if "Email not verified" in text:
            break
        time.sleep(1)
    assert "Email not verified by identity provider" in text, text
    return_tab_to_login(app)
    assert admin_user_entry(harness, UNVERIFIED_EMAIL) is None, (
        "an unverified email claim must not provision"
    )


def test_logout_routes_through_idp_when_configured(harness, app, oidc_stack):
    # ...and with logout-redirect off (the config so far), logout stays
    # in-app: covered by the earlier scenarios' plain app.logout() legs
    enable_sso(harness, oidc_stack, logout_redirect=True)
    at_login(harness, app)
    remount_login(app)
    app.wait_for_text(PROVIDER_LABEL, 30000)

    tap_sso(app, oidc_stack)
    approve_at_idp(oidc_stack, app, 0)
    wait_landed(app, "No workspaces yet")

    # logout now routes the tab through the IdP's end-session endpoint
    # and back to the app's login page. The IdP leg is a sub-second
    # redirect — too fast to catch by polling the tab URL — so the IdP's
    # recorded event (with the redirect target it was handed) is the
    # proof the browser went through it; the landing completes the pair.
    app.tap_label("Logout")
    cdp_wait_tab_url((f"{app_origin()}/#/login",), timeout=60)
    app.wait_for_login_page(timeout_ms=90000)
    end_session = [e for e in oidc_stack.events if e[0] == "end_session"]
    assert end_session, oidc_stack.events
    assert end_session[-1][1] == f"{app_origin()}/#/login", end_session
