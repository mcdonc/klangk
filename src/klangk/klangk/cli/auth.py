"""Login / logout — authenticate and store JWT."""

from __future__ import annotations

import base64
import html
import http.server
import json
import logging
import os
import socket
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
from rich.console import Console
from rich.prompt import Prompt

from .config import CLIState, seed_config
from .transport import http_request

_err = Console(stderr=True)
_out = Console()


UNREACHABLE = "unreachable"


def fetch_config(server_url: str) -> dict | str | None:
    """Fetch /api/v1/config from the server.

    Returns:
        dict — valid klangk config
        UNREACHABLE — server is down or unreachable
        None — server responded but is not a klangk instance
    """
    try:
        resp = http_request(server_url, "GET", "/api/v1/config", timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
        return None
    except httpx.HTTPError:
        return UNREACHABLE


def login_failure_detail(resp) -> str:
    """The server's error detail, or the HTTP status when not JSON."""
    try:
        return resp.json().get("detail", f"HTTP {resp.status_code}")
    except Exception:
        return f"HTTP {resp.status_code}"


def password_expired_message(resp) -> str | None:
    """The expired-password message, or None when the response doesn't
    signal expiry (#3177).

    The server marks the state machine-readably: HTTP 403 with
    ``detail = {"error": "password_expired", "message": ...}``.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return None
    if isinstance(detail, dict) and detail.get("error") == "password_expired":
        return detail.get("message", "Password has expired")
    return None


def local_login(server_url: str) -> tuple[str, str]:
    """No-auth single-user mode: fetch a free token for the seeded default
    user via POST /api/v1/auth/local (#1374).

    Returns ``(email, token)``. Raises ``SystemExit(1)`` on any failure
    (network error, non-200, or missing fields) so callers can treat it
    like the password/OIDC login arms: success returns, failure exits.
    """
    try:
        resp = http_request(
            server_url, "POST", "/api/v1/auth/local", timeout=15.0
        )
    except httpx.HTTPError as exc:
        _err.print(
            f"[red]Error:[/red] could not reach {server_url}"
            f" for no-auth login: {exc}"
        )
        raise SystemExit(1)
    if resp.status_code != 200:
        _err.print(f"[red]Login failed:[/red] {login_failure_detail(resp)}")
        raise SystemExit(1)
    data = resp.json()
    token = data.get("access_token")
    email = data.get("email") or "local"
    if not token:
        _err.print("[red]Login failed:[/red] server returned no access token")
        raise SystemExit(1)
    return email, token


def oidc_browser_login(
    server_url: str,
    provider_id: str,
    state: CLIState,
) -> None:
    """Launch browser for OIDC login, receive token via localhost callback."""
    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    callback_url = f"http://localhost:{port}/callback"
    login_url = (
        f"{server_url}/api/v1/auth/oidc/{provider_id}/login"
        f"?cli_redirect={callback_url}"
    )

    token_holder: list[str | None] = [None]
    email_holder: list[str | None] = [None]
    error_holder: list[str | None] = [None]

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            # #3201: the redirect carries a one-time code, not the JWT;
            # redeem it immediately for the session token so the JWT
            # never touches a URL or browser history.
            code = params.get("code", [None])[0]
            if code:
                try:
                    resp = http_request(
                        server_url,
                        "POST",
                        "/api/v1/auth/oidc/exchange",
                        json={"code": code},
                        timeout=15.0,
                    )
                except httpx.HTTPError:
                    resp = None
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    token_holder[0] = data.get("access_token")
                    email_holder[0] = data.get("email") or "unknown"
                if token_holder[0]:
                    self._send_page(
                        200,
                        "Login Successful",
                        "You are now logged in. You can close this tab.",
                        "#2e7d32",
                    )
                    return
                error_holder[0] = "Login code exchange failed"
                self._send_page(
                    400, "Login Failed", error_holder[0], "#c62828"
                )
            else:
                error = params.get("error", ["Unknown error"])[0]
                error_holder[0] = error
                self._send_page(
                    400,
                    "Login Failed",
                    error,
                    "#c62828",
                )

        def _send_page(self, code, title, message, color):
            safe_title = html.escape(title)
            safe_message = html.escape(message)
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{safe_title}</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;
justify-content:center;align-items:center;min-height:100vh;
margin:0;background:#1a1a2e;color:#e0e0e0">
<div style="text-align:center;max-width:400px;padding:40px">
<div style="font-size:48px;margin-bottom:16px">
{"&#10003;" if code == 200 else "&#10007;"}</div>
<h1 style="color:{color};margin:0 0 12px">{safe_title}</h1>
<p style="color:#aaa;font-size:16px">{safe_message}</p>
</div></body></html>""".encode()
            )

        def log_message(self, format, *args):  # noqa: A002
            pass  # Suppress request logging

    server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)

    # Handle one request then stop
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    _out.print("Opening browser for SSO login...")
    _out.print("[dim]If the browser doesn't open, visit:[/dim]")
    _out.print(f"[dim]{login_url}[/dim]")
    webbrowser.open(login_url)

    # Wait for the callback (timeout after 2 minutes)
    server_thread.join(timeout=120)
    server.server_close()

    if token_holder[0]:
        token = token_holder[0]
        email = email_holder[0] or "unknown"

        state.set_credentials(server_url, email, token)
        state.save()
        seed_config(server_url, email)
        _out.print(f"Logged in as [bold]{email}[/bold]")
    elif error_holder[0]:
        _err.print(f"[red]Login failed:[/red] {error_holder[0]}")
        raise SystemExit(1)
    else:
        _err.print("[red]Login timed out[/red] — no response received")
        raise SystemExit(1)


def cached_user_token(state, server_url: str, email: str) -> str | None:
    """The cached token for *email* on the server, if any."""
    ss = state.servers.get(server_url)
    cached = ss.users.get(email) if ss else None
    if cached and cached.token:
        return cached.token
    return None


def already_logged_in(state, server_url: str, email: str) -> bool:
    """True (and prints) when a cached token for *email* still works."""
    token = cached_user_token(state, server_url, email)
    if not token:
        return False
    try:
        resp = http_request(
            server_url,
            "GET",
            "/api/v1/workspaces",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            state.set_credentials(server_url, email, token)
            state.save()
            _out.print(f"Already logged in as [bold]{email}[/bold]")
            return True
    except httpx.HTTPError:
        pass  # Token invalid or server unreachable — fall through
    return False


def fetch_config_or_exit(server_url: str):
    """Probe the server; exit(1) with a hint if it isn't a klangk instance
    or is unreachable."""
    config = fetch_config(server_url)
    if config is None:
        _err.print(
            f"[red]Error:[/red] {server_url} does not appear to be a"
            " klangk server."
        )
        _err.print(
            "[yellow]Hint:[/yellow] did you forget the subpath?"
            " (e.g. https://host/klangk)"
        )
        raise SystemExit(1)
    if config == UNREACHABLE:
        _err.print(f"[red]Error:[/red] could not reach {server_url}")
        raise SystemExit(1)
    return config


def select_oidc_provider(providers: list) -> dict:
    """The single provider, or prompt when several are configured."""
    if len(providers) == 1:
        return providers[0]
    _out.print("Select an SSO provider:")
    for i, p in enumerate(providers, 1):
        _out.print(f"  {i}. {p['display_name']}")
    choice = Prompt.ask(
        "[bold]Provider[/bold]",
        default="1",
    )
    try:
        idx = int(choice) - 1
        return providers[idx]
    except (ValueError, IndexError):
        _err.print("[red]Invalid choice[/red]")
        raise SystemExit(1)


def should_use_oidc(auth_modes: str, email, password) -> bool:
    """OIDC when password login is off, or no credentials were given."""
    return auth_modes == "oidc" or (email is None and password is None)


def try_oidc_login(server_url, email, password, config, state) -> bool:
    """Run the OIDC browser flow when the server config calls for it.

    Uses OIDC if password login is disabled, or if the user didn't
    explicitly provide email/password credentials. Returns True when the
    flow ran (login is then complete)."""
    providers = config.get("oidc_providers", [])
    auth_modes = config.get("auth_modes", "password")
    if not (providers and auth_modes in ("oidc", "both")):
        return False
    if not should_use_oidc(auth_modes, email, password):
        return False
    provider = select_oidc_provider(providers)
    oidc_browser_login(server_url, provider["id"], state)
    return True


def print_login_failure(resp) -> None:
    """Explain a non-200 login response (redirect hint or server detail)."""
    if resp.status_code in (301, 302, 307, 308):
        location = resp.headers.get("location", "")
        _err.print(f"[red]Login failed:[/red] server redirected to {location}")
        if location.startswith("https://"):
            _err.print("[yellow]Hint:[/yellow] use https:// in the server URL")
    else:
        try:
            detail = resp.json().get("detail", f"HTTP {resp.status_code}")
        except Exception:
            detail = f"HTTP {resp.status_code}"
        _err.print(f"[red]Login failed:[/red] {detail}")
    raise SystemExit(1)


_FORCED_CHANGE_MAX_ATTEMPTS = 5


def _try_change_password(
    server_url: str, token: str, current_password: str
) -> str | None:
    """Prompt once and POST; the new password on success, None on
    failure."""
    new_password = Prompt.ask("[bold]New password[/bold]", password=True)
    confirm = Prompt.ask("[bold]Confirm new password[/bold]", password=True)
    if new_password != confirm:
        _err.print("[red]Passwords do not match.[/red]")
        return None
    resp = http_request(
        server_url,
        "POST",
        "/api/v1/auth/change-password",
        json={
            "current_password": current_password,
            "new_password": new_password,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
    )
    if resp.status_code == 200:
        _out.print("[green]Password changed.[/green]")
        return new_password
    _err.print(
        f"[red]Password change failed:[/red] {login_failure_detail(resp)}"
    )
    return None


def _forced_password_change(
    server_url: str, token: str, current_password: str
) -> str:
    """Prompt for a new password and POST it. Returns the NEW password
    — the change revokes every session of the old credential (#3152),
    so the caller logs in again with it. Exits after
    ``_FORCED_CHANGE_MAX_ATTEMPTS`` failed attempts."""
    _out.print(
        "\n[yellow]Your password was set by an administrator."
        " You must change it now.[/yellow]\n"
    )
    for _ in range(_FORCED_CHANGE_MAX_ATTEMPTS):
        new_password = _try_change_password(
            server_url, token, current_password
        )
        if new_password is not None:
            return new_password
    _err.print("[red]Too many failed attempts. Login aborted.[/red]")
    raise SystemExit(1)


def _fresh_login_token(server_url: str, email: str, password: str) -> str:
    """Re-login after a forced change revoked the old session (#3152,
    #3172). Any failure exits through the normal login-failure path."""
    resp = http_request(
        server_url,
        "POST",
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
        timeout=15.0,
    )
    if resp.status_code != 200:
        print_login_failure(resp)
    return resp.json()["access_token"]


def _login_data(
    server_url: str, email: str, password: str, state
) -> dict | None:
    """The parsed login response, or None when an expired password
    (#3177) was rotated and the login is already persisted. Any other
    failure exits through the login-failure path."""
    resp = http_request(
        server_url,
        "POST",
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
        timeout=15.0,
    )
    if resp.status_code == 200:
        return resp.json()
    expired = password_expired_message(resp)
    if expired is not None:
        token = expired_password_rotation(
            server_url, email, password, notice=expired
        )
        persist_login(state, server_url, email, token)
        return None
    print_login_failure(resp)


def password_login(server_url, email, password, state) -> None:
    """Prompt for credentials (accepts an email or a handle, #616), POST
    them, and persist the returned token. An expired password (#3177)
    routes into the set-new-password flow instead of failing."""
    email = email or Prompt.ask("[bold]Email or handle[/bold]")
    password = password or Prompt.ask("[bold]Password[/bold]", password=True)

    data = _login_data(server_url, email, password, state)
    if data is None:
        return
    token = data["access_token"]

    # #3172: server signals that the password was admin-chosen and must
    # be changed before any other action.
    if data.get("must_change_password"):
        token = _complete_forced_change(server_url, email, password, token)

    persist_login(state, server_url, email, token)


def _complete_forced_change(
    server_url: str, email: str, password: str, token: str
) -> str:
    """Run the forced-change flow and return the token to persist. The
    change revokes the current session (#3152), so the caller cannot
    reuse *token* — a fresh login with the new password mints the
    replacement."""
    new_password = _forced_password_change(server_url, token, password)
    return _fresh_login_token(server_url, email, new_password)


def persist_login(state, server_url, email, token) -> None:
    """Store the token for a completed login and announce it."""
    state.set_credentials(server_url, email, token)
    state.save()
    seed_config(server_url, email)
    _out.print(f"Logged in as [bold]{email}[/bold]")


def expired_password_rotation(
    server_url, identifier, current_password, notice: str
) -> str:
    """Prompt for a replacement password and rotate the expired one
    (#3177). Returns the minted access token; exits on failure."""
    _out.print(f"[yellow]{notice}[/yellow]")
    new_password = prompt_new_password()
    resp = http_request(
        server_url,
        "POST",
        "/api/v1/auth/change-expired-password",
        json={
            "identifier": identifier,
            "current_password": current_password,
            "new_password": new_password,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        print_login_failure(resp)
    return resp.json()["access_token"]


def prompt_new_password() -> str:
    """Ask for a new password twice; re-ask until the entries match."""
    while True:
        first = Prompt.ask("[bold]New password[/bold]", password=True)
        confirm = Prompt.ask(
            "[bold]Confirm new password[/bold]", password=True
        )
        if first == confirm:
            return first
        _err.print("[red]Passwords do not match; try again.[/red]")


def none_mode_login(server_url, state) -> None:
    """Log in via the no-auth free-token arm and persist it."""
    email, token = local_login(server_url)
    state.set_credentials(server_url, email, token)
    state.save()
    seed_config(server_url, email)
    _out.print(f"Logged in as [bold]{email}[/bold] (no-auth mode)")


def login_with_config(
    server_url, email, password, config: dict, state
) -> None:
    """The config-routed login arms: no-auth, OIDC, or password."""
    auth_modes = config.get("auth_modes", "password")
    if auth_modes == "none":
        none_mode_login(server_url, state)
        return
    if try_oidc_login(server_url, email, password, config, state):
        return
    password_login(server_url, email, password, state)


def login(
    server_url: str,
    email: str | None = None,
    password: str | None = None,
) -> None:
    """Prompt for credentials, store JWT in state."""
    state = CLIState.load()

    # If we already have a cached token for this user, verify it.
    if email and already_logged_in(state, server_url, email):
        return

    # Probe the server to verify it's a klangk instance
    config = fetch_config_or_exit(server_url)

    # Default-safe per #1374: a missing config (old server) routes to
    # the password arm rather than the /auth/local arm.
    if config:
        login_with_config(server_url, email, password, config, state)
        return

    password_login(server_url, email, password, state)


def refresh_token(server_url: str, token: str) -> str | None:
    """Exchange *token* for a fresh one via the server's refresh endpoint.

    On success the new token is persisted to klangk-state.yaml and returned.
    Returns ``None`` on any failure (expired, revoked, network error).
    """
    try:
        resp = http_request(
            server_url,
            "POST",
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        new_token = resp.json().get("access_token")
        if not new_token:
            return None
        # Decode email from the new token so we can update state
        try:
            payload = new_token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email", "unknown")
        except Exception:
            email = "unknown"
        state = CLIState.load()
        state.set_credentials(server_url, email, new_token)
        state.save()
        return new_token
    except httpx.HTTPError:
        return None


def proc_cmdline(pid: int) -> str | None:
    """One /proc pid's cmdline, or None when unreadable."""
    try:
        return (
            open(f"/proc/{pid}/cmdline", "rb")
            .read()
            .replace(b"\0", b" ")
            .decode("utf-8", "replace")
            .strip()
        )
    except OSError:
        return None


def proc_ppid(pid: int) -> int | None:
    """One /proc pid's parent pid, or None when unreadable."""
    try:
        return int(open(f"/proc/{pid}/stat", "rb").read().decode().split()[3])
    except (OSError, ValueError):
        return None


def next_chain_pid(cur: int) -> int | None:
    """The next pid up the chain, or None when the walk should stop."""
    ppid = proc_ppid(cur)
    if ppid is None or ppid <= 1 or ppid == cur:
        return None
    return ppid


def logout_process_chain() -> list[str]:
    """The parent-process chain entries (``pid=[cmd]``) up to init."""
    chain: list[str] = []
    cur = os.getppid()
    for _ in range(8):
        cmd = proc_cmdline(cur)
        if cmd is None:
            break
        chain.append(f"{cur}=[{cmd}]")
        cur = next_chain_pid(cur)
        if cur is None:
            break
    return chain


def _log_logout_caller() -> None:
    """Diagnostic: log the parent-process chain that invoked logout.

    Walks /proc upward so the next spurious logout reveals its spawner
    (shell, script, cron, the TUI, ...). Best-effort; never raises.
    """
    msg = "auth.logout() invoked; process chain: " + (
        " <- ".join(logout_process_chain()) or "(unknown)"
    )
    logging.getLogger(__name__).warning(msg)


def logout(server_url: str) -> None:
    """Clear stored credentials for a server."""
    _log_logout_caller()
    state = CLIState.load()
    token = state.get_token(server_url)

    # Clear local state first
    state.clear_credentials(server_url)
    state.save()

    # Then notify server
    if token:
        try:
            http_request(
                server_url,
                "POST",
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except httpx.HTTPError:
            _err.print(
                "[yellow]Logged out locally[/yellow]"
                " — server logout failed (network error)"
            )
            return
    _out.print("Logged out")
