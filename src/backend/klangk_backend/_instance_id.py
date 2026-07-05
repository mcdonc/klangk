"""CLI front-end for reading (or creating) the instance ID.

Registered as the ``klangk-instance-id`` console script in
``src/backend/pyproject.toml``.  Non-Python callers — devenv scripts,
E2E test harnesses, build scripts — shell out to this instead of
querying the database directly.

Usage::

    klangk-instance-id                # prints the instance UUID
    KLANGK_DATA_DIR=/data klangk-instance-id  # uses a custom data dir

The script opens the SQLite database directly (synchronous, no async
engine) to read or create the ``instance_metadata`` row, then prints
the value to stdout.
"""

import sys

from .model.instance import resolve_instance_id_sync


def main() -> None:
    """Print the instance ID to stdout."""
    if len(sys.argv) != 1:
        print("usage: klangk-instance-id", file=sys.stderr)
        raise SystemExit(2)
    sys.stdout.write(resolve_instance_id_sync())


if __name__ == "__main__":  # pragma: no cover
    main()
