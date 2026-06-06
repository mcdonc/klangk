"""Login / logout — authenticate and store JWT."""

from __future__ import annotations

import http.server
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

import httpx
from rich.console import Console
from rich.prompt import Prompt

from .config import CLIConfig

_err = Console(stderr=True)
_out = Console()


def _fetch_config(server_url: str) -> dict | None:
    """Fetch /api/config from the server. Returns None on failure."""
    try:
        resp = httpx.get(f"{server_url}/api/config", timeout=5.0)
        if resp.status_code == 200:
            return resp.json()
    except httpx.HTTPError:
        pass
    return None


def _oidc_browser_login(  # pragma: no cover
    server_url: str,
    provider_id: str,
    cfg: CLIConfig,
) -> None:
    """Launch browser for OIDC login, receive token via localhost callback."""
    # Find a free port
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    callback_url = f"http://localhost:{port}/callback"
    login_url = (
        f"{server_url}/auth/oidc/{provider_id}/login"
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
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Login successful!</h2>"
                    b"<p>You can close this tab.</p></body></html>"
                )
            else:
                error = params.get("error", ["Unknown error"])[0]
                error_holder[0] = error
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"<html><body><h2>Login failed</h2>"
                    f"<p>{error}</p></body></html>".encode()
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
            import base64
            import json

            payload = token.split(".")[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email", "unknown")
        except Exception:
            email = "unknown"

        cfg.server.url = server_url
        cfg.auth.token = token
        cfg.auth.email = email
        cfg.save()
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
    """Prompt for credentials, store JWT in config."""
    cfg = CLIConfig.load()

    # If we already have a token for this server, verify it first.
    if cfg.auth.token and cfg.server.url == server_url:
        try:
            resp = httpx.get(
                f"{server_url}/workspaces",
                headers={"Authorization": f"Bearer {cfg.auth.token}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                _out.print(
                    f"Already logged in as"
                    f" [bold]{cfg.auth.email or 'unknown'}[/bold]"
                )
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

                _oidc_browser_login(server_url, provider["id"], cfg)
                return

    # Fall through to password login
    email = email or Prompt.ask("[bold]Email[/bold]")
    password = password or Prompt.ask("[bold]Password[/bold]", password=True)

    resp = httpx.post(
        f"{server_url}/auth/login",
        json={"email": email, "password": password},
        timeout=15.0,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text or f"HTTP {resp.status_code}"
        _err.print(f"[red]Login failed:[/red] {detail}")
        raise SystemExit(1)

    token = resp.json()["access_token"]

    cfg.server.url = server_url
    cfg.auth.token = token
    cfg.auth.email = email
    cfg.save()
    _out.print(f"Logged in as [bold]{email}[/bold]")


def logout() -> None:
    """Clear stored token."""
    cfg = CLIConfig.load()
    if cfg.auth.token:
        token = cfg.auth.token
        # Clear local state first, then notify server.
        cfg.auth.token = None
        cfg.auth.email = None
        cfg.save()
        try:
            httpx.post(
                f"{cfg.server.url}/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        except httpx.HTTPError:
            _err.print(
                "[yellow]Logged out locally[/yellow]"
                " — server logout failed (network error)"
            )
            return
    else:
        cfg.save()
    _out.print("Logged out")
