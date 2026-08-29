#!/usr/bin/env python3
"""Live, rich-rendered view of a workspace's egress-consent history (#2242).

Reads the klangkd SQLite DB read-only and refreshes every second, so you can
watch consent requests arrive (pending) and resolve (allowed / denied /
expired) as the sidecar forwards blocked destinations. A server-side debug
tool -- run it on the klangkd host where the DB lives.

Usage:
  scripts/consent-watch.py <workspace-id> [--data-dir DIR] [--interval 1.0]

``--data-dir`` defaults to ``$KLANGKD_DATA_DIR`` (the dir holding
``klangk.db``); the workspace id may be a full id or a prefix.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from consentlib import data_dir

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

_DECISION_STYLE = {
    "pending": "bold yellow",
    "allowed": "bold green",
    "denied": "bold red",
    "expired": "dim",
}


def _fetch(conn: sqlite3.Connection, ws: str) -> list[tuple]:
    cur = conn.execute(
        "SELECT dest_host, dest_port, decision, scope, requested_at,"
        " decided_at, decided_by FROM egress_consent"
        " WHERE workspace_id = ? OR workspace_id LIKE ?"
        " ORDER BY requested_at DESC LIMIT 200",
        (ws, ws + "%"),
    )
    return cur.fetchall()


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def _render(rows: list[tuple], ws: str) -> Table:
    table = Table(
        title=f"egress consent -- workspace {ws[:8]}",
        caption="live (Ctrl-C to quit)",
    )
    table.add_column("destination")
    table.add_column("decision")
    table.add_column("requested")
    table.add_column("decided")
    table.add_column("scope")
    table.add_column("by")
    if not rows:
        table.add_row(Text("no requests yet", style="dim"), "", "", "", "", "")
        return table
    for host, port, decision, scope, req_at, dec_at, by in rows:
        dest = f"{host}:{port}" if port else host
        table.add_row(
            dest,
            Text(decision, style=_DECISION_STYLE.get(decision, "")),
            _clock(req_at),
            _clock(dec_at),
            scope or "",
            by or "",
        )
    return table


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workspace_id", help="workspace id (full or prefix)")
    ap.add_argument(
        "--data-dir",
        default=None,
        help="klangkd data dir containing klangk.db (default: $KLANGKD_DATA_DIR)",
    )
    ap.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    args = ap.parse_args()

    db_path = data_dir(args.data_dir) / "klangk.db"
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    console = Console()
    ws = args.workspace_id
    try:
        with Live(_render([], ws), console=console) as live:
            while True:
                live.update(_render(_fetch(conn, ws), ws))
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
