#!/usr/bin/env python3
"""A tiny CLI todo app backed by SQLite."""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("~/.todo.db").expanduser()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS todos ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task TEXT NOT NULL,"
        "  done INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.commit()
    return conn


def add(task: str):
    db = _db()
    db.execute("INSERT INTO todos (task) VALUES (?)", (task,))
    db.commit()
    print(f"Added: {task}")


def ls():
    db = _db()
    rows = db.execute("SELECT id, task, done FROM todos ORDER BY id").fetchall()
    if not rows:
        print("No todos.")
        return
    for row_id, task, done in rows:
        mark = "x" if done else " "
        print(f"  [{mark}] {row_id}. {task}")


def done(todo_id: int):
    db = _db()
    db.execute("UPDATE todos SET done = 1 WHERE id = ?", (todo_id,))
    db.commit()
    print(f"Marked #{todo_id} done.")


def rm(todo_id: int):
    db = _db()
    db.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    db.commit()
    print(f"Removed #{todo_id}.")


def main():
    if len(sys.argv) < 2:
        print("Usage: todo.py <add|ls|done|rm> [args]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) >= 3:
        add(" ".join(sys.argv[2:]))
    elif cmd == "ls":
        ls()
    elif cmd == "done" and len(sys.argv) == 3:
        done(int(sys.argv[2]))
    elif cmd == "rm" and len(sys.argv) == 3:
        rm(int(sys.argv[2]))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
