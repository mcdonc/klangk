"""Idempotent fixture seed for the E2E Gitea instance (#3385).

Run via ``gitea-e2e seed`` (scripts/gitea-e2e.sh); the instance must
already be listening. Creates, reusing what already exists on re-runs:

- the admin user ``gitea-admin`` / ``gitea-e2e-admin`` (registration is
  closed, so the seed is the only account source),
- an API token (scopes: all), verified per run and regenerated only when
  it stops working — stored in ``<state>/token.txt``,
- the public auto-initialized repo ``gitea-admin/e2e-repo`` plus the
  PRIVATE ``gitea-admin/e2e-private-repo`` (git only asks the credential
  helper for a private repo — that is the OAuth flow's trigger),
- the OAuth2 application ``klangk-e2e``: a public client (PKCE-capable —
  confidential clients fail the PKCE challenge on Gitea >= 1.22,
  go-gitea/gitea#33956) whose redirect URI comes from the caller. A
  redirect change re-registers the application (Gitea matches redirect
  URIs exactly and they are immutable per application).

Everything an E2E suite needs lands in ``<state>/credentials`` plus
machine-readable ``<state>/oauth2.json`` and ``<state>/token.txt``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ADMIN_USER = "gitea-admin"
ADMIN_PASSWORD = "gitea-e2e-admin"
ADMIN_EMAIL = "gitea-admin@example.com"
REPO_NAME = "e2e-repo"
# The credential-demanding clone target: git only asks the helper for a
# PRIVATE repo, so the OAuth E2E clones this one (#3385).
PRIVATE_REPO_NAME = "e2e-private-repo"
APP_NAME = "klangk-e2e"
TOKEN_HEX = re.compile(r"\b[0-9a-f]{40}\b")
READY_TIMEOUT = 60


def _error_detail(exc: urllib.error.HTTPError) -> tuple[int, dict | list | None]:
    """Parse an HTTPError body so failures name the server's reason."""
    try:
        return exc.code, json.loads(exc.read() or b"null")
    except (json.JSONDecodeError, ValueError):
        return exc.code, None


def _request_headers(token: str | None, json_body: bool) -> dict[str, str]:
    """Auth + content-type headers; the JSON content-type is required —
    urllib sends none on its own and Gitea's binding answers 422
    without it."""
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def api(
    base: str, method: str, path: str, token: str | None, body: dict | None
) -> tuple[int, dict | list | None]:
    """One API call; returns (status, parsed-json-or-None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers=_request_headers(token, data is not None),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            parsed = json.loads(resp.read() or b"null")
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        return _error_detail(exc)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return 0, None


def gitea_cli(state: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a gitea admin subcommand against this instance's config."""
    return subprocess.run(
        ["gitea", "--config", str(state / "app.ini"), "--work-path", str(state), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(state / "home")},
    )


def _write_private(path: Path, text: str) -> None:
    """Write a state file readable only by its owner — it carries the
    fixture's API token and OAuth client."""
    path.write_text(text)
    os.chmod(path, 0o600)


def admin_exists(state: Path) -> bool:
    listing = gitea_cli(state, "admin", "user", "list")
    return ADMIN_USER in listing.stdout


def ensure_admin(state: Path) -> None:
    if admin_exists(state):
        return
    created = gitea_cli(
        state,
        "admin",
        "user",
        "create",
        "--admin",
        "--username",
        ADMIN_USER,
        "--password",
        ADMIN_PASSWORD,
        "--email",
        ADMIN_EMAIL,
        "--must-change-password=false",
    )
    if created.returncode != 0:
        sys.exit(f"gitea-e2e: admin user create failed: {created.stderr.strip()}")


def ensure_token(state: Path, base: str) -> str:
    """Reuse the stored token while it still authenticates."""
    token_file = state / "token.txt"
    if token_file.is_file():
        token = token_file.read_text().strip()
        status, _ = api(base, "GET", "/api/v1/user", token, None)
        if status == 200:
            return token
    generated = gitea_cli(
        state,
        "admin",
        "user",
        "generate-access-token",
        "--username",
        ADMIN_USER,
        "--scopes",
        "all",
        "--token-name",
        f"e2e-{int(time.time())}",
    )
    match = TOKEN_HEX.search(generated.stdout)
    if generated.returncode != 0 or not match:
        sys.exit(f"gitea-e2e: token generation failed: {generated.stderr.strip()}")
    token = match.group(0)
    _write_private(state / "token.txt", token + "\n")
    return token


def ensure_repo(base: str, token: str, name: str, private: bool) -> None:
    status, _ = api(base, "GET", f"/api/v1/repos/{ADMIN_USER}/{name}", token, None)
    if status == 200:
        return
    status, detail = api(
        base,
        "POST",
        "/api/v1/user/repos",
        token,
        {
            "name": name,
            "private": private,
            "auto_init": True,
            "description": "clone target for klangk E2E (#3385)",
        },
    )
    if status not in (201, 409):
        sys.exit(f"gitea-e2e: repo create failed (HTTP {status}): {detail}")


def existing_app(base: str, token: str) -> dict | None:
    status, apps = api(base, "GET", "/api/v1/user/applications/oauth2", token, None)
    if status != 200 or not isinstance(apps, list):
        return None
    for app in apps:
        if app.get("name") == APP_NAME:
            return app
    return None


def create_app(base: str, token: str, redirect: str) -> dict:
    status, app = api(
        base,
        "POST",
        "/api/v1/user/applications/oauth2",
        token,
        {
            "name": APP_NAME,
            "redirect_uris": [redirect],
            # A public client: PKCE works (go-gitea/gitea#33956 —
            # confidential clients fail the PKCE challenge since 1.22).
            "confidential_client": False,
        },
    )
    if status != 201 or not isinstance(app, dict):
        sys.exit(f"gitea-e2e: OAuth app create failed (HTTP {status}): {app}")
    return app


def ensure_oauth_app(base: str, token: str, redirect: str) -> dict:
    """Reuse the app when its redirect matches; re-create when it moved."""
    app = existing_app(base, token)
    if app is not None:
        if redirect in app.get("redirect_uris", []):
            return app
        api(
            base, "DELETE", f"/api/v1/user/applications/oauth2/{app['id']}", token, None
        )
    return create_app(base, token, redirect)


def wait_ready(base: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        status, _ = api(base, "GET", "/api/v1/version", None, None)
        if status == 200:
            return
        time.sleep(1)
    sys.exit(f"gitea-e2e: {base} never became ready ({READY_TIMEOUT}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--redirect-uri", required=True)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    wait_ready(base)
    ensure_admin(args.state)
    token = ensure_token(args.state, base)
    ensure_repo(base, token, REPO_NAME, False)
    ensure_repo(base, token, PRIVATE_REPO_NAME, True)
    app = ensure_oauth_app(base, token, args.redirect_uri)

    _write_private(
        args.state / "oauth2.json",
        json.dumps(
            {
                "client_id": app["client_id"],
                "client_secret": app.get("client_secret", ""),
                "redirect_uri": args.redirect_uri,
                "confidential_client": False,
            },
            indent=2,
        )
        + "\n",
    )
    _write_private(
        args.state / "credentials",
        "\n".join(
            (
                f"url:       {base}",
                f"repo:      {base}/{ADMIN_USER}/{REPO_NAME}.git",
                f"private:   {base}/{ADMIN_USER}/{PRIVATE_REPO_NAME}.git",
                f"admin:     {ADMIN_USER} / {ADMIN_PASSWORD}",
                f"api token: {token}",
                f"oauth2 client_id:     {app['client_id']}",
                f"oauth2 client_secret: {app.get('client_secret', '(public client: none)')}",
                f"oauth2 redirect_uri:  {args.redirect_uri}",
                "oauth2 endpoints:",
                f"  authorize: {base}/login/oauth/authorize",
                f"  token:     {base}/login/oauth/access_token",
                "",
            )
        ),
    )
    print(f"gitea-e2e: seeded {base} (gitea-e2e info for credentials)")


if __name__ == "__main__":
    main()
