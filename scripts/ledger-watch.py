#!/usr/bin/env python3
"""Live, rich-rendered view of a workspace's process-launch ledger (#2520).

Reads the klangkd SQLite DB read-only and refreshes every second, so you can
watch launches arrive as the ledger captures them (agent vs user
attribution visible live). A server-side debug tool -- run it on the
klangkd host where the DB lives.

Usage:
  scripts/ledger-watch.py <workspace-id> [--data-dir DIR] [--interval 1.0]
                          [--all] [--principal PRINCIPAL]

``--data-dir`` defaults to ``$KLANGKD_DATA_DIR`` (the dir holding
``klangk.db``); the workspace id may be a full id or a prefix. ``--all``
watches every workspace (rows are prefixed with the workspace id);
``--principal`` filters by exact principal (``agent``, ``user:alice``,
``unknown``).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

_PRINCIPAL_STYLE = {
    "agent": "bold magenta",
    "unknown": "bold red",
}
_KIND_STYLE = {
    "exec": "cyan",
}

_QUERY_ALL = (
    "SELECT workspace_id, pid, ppid, uid, comm, argv, started_at,"
    " principal, attribution_method, pane_hint, event_kind"
    " FROM process_launch"
    " WHERE workspace_id = ? OR workspace_id LIKE ?"
)
_QUERY_EVERY = (
    "SELECT workspace_id, pid, ppid, uid, comm, argv, started_at,"
    " principal, attribution_method, pane_hint, event_kind"
    " FROM process_launch"
)


def _data_dir(arg: str | None) -> Path:
    path = arg or os.environ.get("KLANGKD_DATA_DIR")
    if not path:
        sys.exit(
            "data dir not set: pass --data-dir or export KLANGKD_DATA_DIR "
            "(the klangkd dir containing klangk.db)"
        )
    return Path(path)


def _fetch(
    conn: sqlite3.Connection,
    ws: str,
    *,
    watch_all: bool,
    principal: str | None,
    limit: int,
) -> list[tuple]:
    if watch_all:
        cur = conn.execute(_QUERY_EVERY)
    else:
        cur = conn.execute(_QUERY_ALL, (ws, ws + "%"))
    rows = cur.fetchall()
    if principal is not None:
        rows = [r for r in rows if r[7] == principal]
    rows.sort(key=lambda r: r[6], reverse=True)
    return rows[:limit]


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def _principal_text(principal: str) -> Text:
    if principal.startswith("user:"):
        return Text(principal, style="bold green")
    return Text(principal, style=_PRINCIPAL_STYLE.get(principal, ""))


def _render(rows: list[tuple], ws: str, *, watch_all: bool) -> Table:
    title = (
        "process launches -- all workspaces"
        if watch_all
        else f"process launches -- workspace {ws[:8]}"
    )
    table = Table(title=title, caption="live (Ctrl-C to quit)")
    if watch_all:
        table.add_column("ws", style="dim")
    table.add_column("pid")
    table.add_column("ppid")
    table.add_column("comm")
    table.add_column("argv", overflow="fold")
    table.add_column("principal")
    table.add_column("method")
    table.add_column("hint")
    table.add_column("kind")
    table.add_column("at")
    if not rows:
        blank = Text("no launches captured yet", style="dim")
        pre = ("",) if watch_all else ()
        table.add_row(*pre, blank, *([""] * 9))
        return table
    for (
        ws_id,
        pid,
        ppid,
        uid,
        comm,
        argv,
        started_at,
        principal,
        method,
        pane_hint,
        kind,
    ) in rows:
        pre = (ws_id[:8],) if watch_all else ()
        table.add_row(
            *pre,
            str(pid),
            str(ppid) if ppid is not None else "",
            comm or "",
            argv or "",
            _principal_text(principal),
            method or "",
            pane_hint or "",
            Text(kind, style=_KIND_STYLE.get(kind, "")),
            _clock(started_at),
        )
    return table


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "workspace_id",
        nargs="?",
        default=None,
        help="workspace id (full or prefix); optional with --all",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="watch every workspace (rows prefixed with the workspace id)",
    )
    ap.add_argument(
        "--principal",
        default=None,
        help="filter by exact principal (agent, user:<handle>, unknown)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=200,
        help="rows to show (newest first)",
    )
    ap.add_argument(
        "--data-dir",
        default=None,
        help="klangkd data dir containing klangk.db (default: $KLANGKD_DATA_DIR)",
    )
    ap.add_argument("--interval", type=float, default=1.0, help="refresh seconds")
    args = ap.parse_args()
    if not args.all and not args.workspace_id:
        ap.error("workspace_id is required unless --all is given")

    db_path = _data_dir(args.data_dir) / "klangk.db"
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    # Fail fast with a readable reason when the table is absent (ledger
    # never enabled on this DB) instead of an sqlite stack trace.
    try:
        conn.execute("SELECT 1 FROM process_launch LIMIT 1")
    except sqlite3.OperationalError as exc:
        conn.close()
        sys.exit(
            f"process_launch table unavailable ({exc}); is the ledger "
            "enabled? (KLANGKD_PROCESS_LEDGER_ENABLED=true)"
        )
    console = Console()
    try:
        with Live(
            _render([], args.workspace_id or "", watch_all=args.all), console=console
        ) as live:
            while True:
                rows = _fetch(
                    conn,
                    args.workspace_id or "",
                    watch_all=args.all,
                    principal=args.principal,
                    limit=args.limit,
                )
                live.update(_render(rows, args.workspace_id or "", watch_all=args.all))
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
