"""Login / logout — authenticate and store JWT."""

from __future__ import annotations

import base64
import html
import http.server
import json
import socket
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
from rich.console import Console
from rich.prompt import Prompt

from .config import CLIState

_err = Console(stderr=True)
_out = Console()


def _fetch_config(server_url: str) -> dict | None:
    """Fetch /api/v1/config from the server. Returns None on failure."""
    try:
        resp = httpx.get(f"{server_url}/api/v1/config", timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


def _oidc_browser_login(  # pragma: no cover
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
    error_holder: list[str | None] = [None]

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            token = params.get("token", [None])[0]
            if token:
                token_holder[0] = token
                self._send_page(
                    200,
                    "Login Successful",
                    "You are now logged in. You can close this tab.",
                    "#2e7d32",
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
        # Decode the JWT to get the email
        try:
            payload = token.split(".")[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email", "unknown")
        except Exception:
            email = "unknown"

        state.set_credentials(server_url, email, token)
        state.save()
        _out.print(f"Logged in as [bold]{email}[/bold]")
    elif error_holder[0]:
        _err.print(f"[red]Login failed:[/red] {error_holder[0]}")
        raise SystemExit(1)
    else:
        _err.print("[red]Login timed out[/red] — no response received")
        raise SystemExit(1)


def login(
    server_url: str,
    email: str | None = None,
    password: str | None = None,
) -> None:
    """Prompt for credentials, store JWT in state."""
    state = CLIState.load()

    # If we already have a cached token for this user, verify it.
    if email:
        ss = state.servers.get(server_url)
        cached = ss.users.get(email) if ss else None
        if cached and cached.token:
            try:
                resp = httpx.get(
                    f"{server_url}/api/v1/workspaces",
                    headers={"Authorization": f"Bearer {cached.token}"},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    state.set_credentials(server_url, email, cached.token)
                    state.save()
                    _out.print(f"Already logged in as [bold]{email}[/bold]")
                    return
            except httpx.HTTPError:
                pass  # Token invalid or server unreachable — fall through

    # Check server config for OIDC providers
    config = _fetch_config(server_url)
    if config:
        providers = config.get("oidc_providers", [])
        auth_modes = config.get("auth_modes", "password")

        if providers and auth_modes in ("oidc", "both"):
            # Use OIDC if password login is disabled, or if the user
            # didn't explicitly provide email/password credentials
            use_oidc = auth_modes == "oidc" or (
                email is None and password is None
            )
            if use_oidc:
                if len(providers) == 1:
                    provider = providers[0]
                else:
                    _out.print("Select an SSO provider:")
                    for i, p in enumerate(providers, 1):
                        _out.print(f"  {i}. {p['display_name']}")
                    choice = Prompt.ask(
                        "[bold]Provider[/bold]",
                        default="1",
                    )
                    try:
                        idx = int(choice) - 1
                        provider = providers[idx]
                    except (ValueError, IndexError):
                        _err.print("[red]Invalid choice[/red]")
                        raise SystemExit(1)

                _oidc_browser_login(server_url, provider["id"], state)
                return

    # Fall through to password login
    email = email or Prompt.ask("[bold]Email[/bold]")
    password = password or Prompt.ask("[bold]Password[/bold]", password=True)

    resp = httpx.post(
        f"{server_url}/api/v1/auth/login",
        json={"email": email, "password": password},
        timeout=15.0,
    )
    if resp.status_code != 200:
        if resp.status_code in (301, 302, 307, 308):
            location = resp.headers.get("location", "")
            _err.print(
                f"[red]Login failed:[/red] server redirected to {location}"
            )
            if location.startswith("https://"):
                _err.print(
                    "[yellow]Hint:[/yellow] use https:// in the server URL"
                )
        else:
            try:
                detail = resp.json().get("detail", f"HTTP {resp.status_code}")
            except Exception:
                detail = f"HTTP {resp.status_code}"
            _err.print(f"[red]Login failed:[/red] {detail}")
        raise SystemExit(1)

    token = resp.json()["access_token"]

    state.set_credentials(server_url, email, token)
    state.save()
    _out.print(f"Logged in as [bold]{email}[/bold]")


def logout(server_url: str) -> None:
    """Clear stored credentials for a server."""
    state = CLIState.load()
    token = state.get_token(server_url)

    # Clear local state first
    state.clear_credentials(server_url)
    state.save()

    # Then notify server
    if token:
        try:
            httpx.post(
                f"{server_url}/api/v1/auth/logout",
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
