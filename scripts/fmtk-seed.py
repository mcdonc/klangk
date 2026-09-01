"""Seed the fmtk verification fixture against a klangkd instance (#2881).

Wired as the ``fmtk-seed`` devenv script; run automatically by
``scripts/fmtk-up.sh`` (which boots the scratch backend this targets), or
standalone against any backend via ``--url``.

Creates, idempotently, five fixture users covering every role bucket of
the ``fmtk-verify`` workspace's Sharing panel:

  ================== ==========================  =========================
  user               grants                     exercises
  ================== ==========================  =========================
  fmtk-admin         ``admins`` group member;   everything: Sharing tab
                     owns the workspace         with role buckets AND
                                                the Advanced ACL editor
  fmtk-collaborator  collaborators role group   Collaborators bucket
  fmtk-coder         coders role group          Coders bucket
  fmtk-spectator     spectators role group      Spectators bucket; no
                                                Sharing tab
  ================== ==========================  =========================

The workspace is created with fmtk-admin's own token, so fmtk-admin holds
the owner wildcard ACE and sees it under "Owned by Me" (``GET
/workspaces`` lists only owned workspaces). Every role group carries
``terminal``, so each member opens the workspace page (the WS
``workspace_connect`` gate requires it — a member whose only grants omit
``terminal`` gets "Permission denied" before any tab renders).

All fixture users share the password ``fmtk-Pass123!``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

WORKSPACE_NAME = "fmtk-verify"
FIXTURE_PASSWORD = "fmtk-Pass123!"
FIXTURES = (
    "fmtk-admin",
    "fmtk-collaborator",
    "fmtk-coder",
    "fmtk-spectator",
)
# fixture user suffix -> workspace role group (bucket membership)
ROLE_MEMBERSHIPS = {
    "fmtk-collaborator": "collaborators",
    "fmtk-coder": "coders",
    "fmtk-spectator": "spectators",
}


def api(base: str, token: str, method: str, path: str, body=None):
    """One JSON API call; returns (status, parsed_json_or_text)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def login(base: str, email: str, password: str) -> str:
    status, body = api(
        base,
        "",
        "POST",
        "/api/v1/auth/login",
        {"identifier": email, "password": password},
    )
    if status != 200:
        sys.exit(f"login as {email} failed ({status}): {body}")
    return body["access_token"]


def ensure_users(base: str, token: str) -> dict[str, str]:
    """Create missing fixture users; returns email -> user_id."""
    _, listing = api(base, token, "GET", "/api/v1/users")
    ids = {u["email"]: u["id"] for u in listing["users"]}
    for name in FIXTURES:
        email = f"{name}@example.com"
        if email in ids:
            continue
        status, body = api(
            base,
            token,
            "POST",
            "/api/v1/users",
            {"email": email, "password": FIXTURE_PASSWORD},
        )
        if status != 200:
            sys.exit(f"create {email} failed ({status}): {body}")
        ids[email] = body["id"]
        print(f"created user {email}")
    return ids


def ensure_admin_group_member(base: str, token: str, user_id: str) -> None:
    """Idempotently add fmtk-admin to the ``admins`` group."""
    _, listing = api(base, token, "GET", "/api/v1/groups?page_size=100")
    group = next(g for g in listing["groups"] if g["name"] == "admins")
    _, members = api(base, token, "GET", f"/api/v1/groups/{group['id']}/members")
    if any(m["id"] == user_id for m in members):
        return
    status, body = api(
        base,
        token,
        "POST",
        f"/api/v1/groups/{group['id']}/members",
        {"user_id": user_id},
    )
    if status != 200:
        sys.exit(f"add admin-group member failed ({status}): {body}")
    print("added fmtk-admin to the admins group")


def ensure_workspace(base: str, owner_token: str) -> str:
    """Find (or create) the fixture workspace as its owner; returns id."""
    _, mine = api(base, owner_token, "GET", "/api/v1/workspaces")
    for ws in mine:
        if ws["name"] == WORKSPACE_NAME:
            return ws["id"]
    status, body = api(
        base,
        owner_token,
        "POST",
        "/api/v1/workspaces",
        {"name": WORKSPACE_NAME},
    )
    if status != 200:
        sys.exit(f"create workspace failed ({status}): {body}")
    print(f"created workspace {WORKSPACE_NAME} (owned by fmtk-admin)")
    return body["id"]


def ensure_role_member(
    base: str, token: str, ws_id: str, role: str, email: str
) -> None:
    """Idempotently add ``email`` to the workspace's ``role`` bucket."""
    _, roles = api(base, token, "GET", f"/api/v1/workspaces/{ws_id}/roles")
    bucket = next(r for r in roles if r["role"] == role)
    if any(m["email"] == email for m in bucket["members"]):
        return
    status, body = api(
        base,
        token,
        "POST",
        f"/api/v1/workspaces/{ws_id}/roles/{role}",
        {"email": email},
    )
    if status != 200:
        sys.exit(f"add {email} to {role} failed ({status}): {body}")
    print(f"added {email} to {role}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8998",
        help="backend base URL (default: the fmtk-up scratch backend)",
    )
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument("--admin-password", default="admin123abc")
    args = parser.parse_args()

    token = login(args.url, args.admin_email, args.admin_password)
    ids = ensure_users(args.url, token)
    ensure_admin_group_member(args.url, token, ids["fmtk-admin@example.com"])
    owner_token = login(args.url, "fmtk-admin@example.com", FIXTURE_PASSWORD)
    ws_id = ensure_workspace(args.url, owner_token)
    for name, role in ROLE_MEMBERSHIPS.items():
        ensure_role_member(args.url, owner_token, ws_id, role, f"{name}@example.com")
    print(
        f"\nfixture ready on {args.url} — workspace {WORKSPACE_NAME} "
        f"({ws_id[:8]}…)\n"
        f"logins (password {FIXTURE_PASSWORD} for all):\n"
        "  fmtk-admin@example.com          -> admins group + owner:"
        " everything\n"
        "  fmtk-collaborator@example.com   -> collaborators bucket\n"
        "  fmtk-coder@example.com          -> coders bucket\n"
        "  fmtk-spectator@example.com      -> spectators bucket, no Sharing"
        " tab"
    )


if __name__ == "__main__":
    main()
