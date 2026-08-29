#!/usr/bin/env python3
"""Interactive CLI to accept/deny a workspace's pending egress-consent
requests (#2242, #2244's command-line decide flow).

Reads the klangkd SQLite DB, lists the workspace's ``pending`` requests, and
prompts [a]ccept / [d]eny / [s]kip for each. Accept marks the request
``allowed`` and adds the destination to the workspace's ``allowed_domains``
(so it's permitted going forward, after the workspace is recreated); deny
marks it ``denied``. ``decided_by`` is the user named by ``--as`` (default:
the admin user) -- never NULL, so a human decision isn't confused with a
static-mode auto-deny. Polls for new pending requests; Ctrl-C to quit.

Usage:
  scripts/consent-decide.py <workspace-id> [--data-dir DIR] [--as EMAIL]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sqlite3
import sys
import time
from consentlib import data_dir

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def _resolve_user(conn: sqlite3.Connection, email: str) -> str:
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        sys.exit(f"no user with email {email!r} (set --as to a valid user)")
    return row[0]


def _fetch_pending(conn: sqlite3.Connection, ws: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, dest_host, dest_port, requested_at"
        " FROM egress_consent"
        " WHERE (workspace_id = ? OR workspace_id LIKE ?)"
        " AND decision = 'pending'"
        " ORDER BY requested_at ASC",
        (ws, ws + "%"),
    ).fetchall()


def _dest(row: sqlite3.Row) -> str:
    return (
        f"{row['dest_host']}:{row['dest_port']}"
        if row["dest_port"]
        else row["dest_host"]
    )


def _decide(
    conn: sqlite3.Connection,
    request_id: str,
    decision: str,
    user_id: str,
    scope: str | None = None,
) -> None:
    conn.execute(
        "UPDATE egress_consent"
        " SET decision = ?, scope = ?, decided_at = ?, decided_by = ?"
        " WHERE id = ? AND decision = 'pending'",
        (decision, scope, time.time(), user_id, request_id),
    )


def _allow_destination(conn: sqlite3.Connection, ws: str, row: sqlite3.Row) -> bool:
    """Add the request's destination to the workspace's allowed_domains.

    Returns True if added, False if it was already present. Applies on the
    next workspace recreate (the sidecar's ruleset is set at create time).
    """
    wrow = conn.execute(
        "SELECT id, allowed_domains FROM workspaces WHERE id = ? OR id LIKE ?",
        (ws, ws + "%"),
    ).fetchone()
    if wrow is None:
        return False
    domains = json.loads(wrow["allowed_domains"] or "[]")
    # Raw IPs must be CIDR (ip/32) so the sidecar entrypoint's static allow
    # loop applies them; a bare IP is neither matched by that loop nor by the
    # DNS proxy (which matches query names). Domains stay as host specs.
    host = row["dest_host"]
    port = row["dest_port"]
    try:
        ipaddress.ip_address(host)
        spec = f"{host}/32"
    except ValueError:
        spec = host
    entry = f"{spec}:{port}" if port else spec
    if entry in domains:
        return False
    domains.append(entry)
    conn.execute(
        "UPDATE workspaces SET allowed_domains = ? WHERE id = ?",
        (json.dumps(domains), wrow["id"]),
    )
    return True


def _display(row: sqlite3.Row) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_row("destination", _dest(row))
    table.add_row(
        "requested", time.strftime("%H:%M:%S", time.localtime(row["requested_at"]))
    )
    console.print(Panel(table, title=f"[bold]{row['id'][:8]}[/bold]", expand=False))


def _prompt_and_decide(conn, ws: str, row, user_id: str, seen: set) -> None:
    """Show one pending request, prompt for a/d/s, and apply the verdict."""
    _display(row)
    choice = (
        console.input(
            "[bold][[a]][/bold]ccept / [bold][[d]][/bold]eny / [bold][[s]][/bold]kip > "
        )
        .strip()
        .lower()
    )
    if choice == "a":
        _decide(conn, row["id"], "allowed", user_id, "workspace")
        added = _allow_destination(conn, ws, row)
        conn.commit()
        seen.add(row["id"])
        console.print(
            f"[green]allowed[/green] {_dest(row)}"
            + (
                " (added to allow-list; recreate the workspace to apply)"
                if added
                else " (already in allow-list)"
            )
        )
    elif choice == "d":
        _decide(conn, row["id"], "denied", user_id)
        conn.commit()
        seen.add(row["id"])
        console.print(f"[red]denied[/red] {_dest(row)}")
    else:
        seen.add(row["id"])
        console.print("skipped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workspace_id", help="workspace id (full or prefix)")
    ap.add_argument(
        "--data-dir",
        default=None,
        help="klangkd data dir containing klangk.db (default: $KLANGKD_DATA_DIR)",
    )
    ap.add_argument(
        "--as",
        dest="as_user",
        default="admin@example.com",
        help="email of the deciding user (default: admin@example.com)",
    )
    args = ap.parse_args()

    db_path = data_dir(args.data_dir) / "klangk.db"
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    user_id = _resolve_user(conn, args.as_user)
    ws = args.workspace_id
    seen: set[str] = set()
    try:
        console.print(
            f"[bold]egress consent decide[/bold] -- workspace {ws[:8]} "
            f"(decider: {args.as_user})"
        )
        while True:
            pending = [r for r in _fetch_pending(conn, ws) if r["id"] not in seen]
            if not pending:
                sys.stdout.write("\rno pending requests; polling (Ctrl-C to quit)   ")
                sys.stdout.flush()
                time.sleep(2)
                continue
            sys.stdout.write("\r" + " " * 50 + "\r")
            for row in pending:
                _prompt_and_decide(conn, ws, row, user_id, seen)
    except KeyboardInterrupt:
        console.print("\nbye.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
