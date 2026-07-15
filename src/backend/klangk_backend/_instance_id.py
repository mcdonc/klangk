"""CLI front-end for reading the instance ID.

Registered as the ``klangk-instance-id`` console script in
``src/backend/pyproject.toml``.  Non-Python callers — devenv scripts,
E2E test harnesses, build scripts — read this instead of opening the
database directly.

It reads ``<data_dir>/instance-id`` — the file klangkd writes at startup
(#1553). It does **not** open the SQLite DB and does **not** generate an
ID when the file is absent: only klangkd writes the truth, so a missing
file means klangkd has not booted yet (or this is a first boot before the
server has run). The script resolves ``data_dir`` from the environment
(``KLANGK_DATA_DIR`` / ``KLANGK_STATE_DIR``), so callers must run with
the same environment klangkd was launched with.

Usage::

    klangk-instance-id                # prints the instance UUID

Exit codes: 0 on success, 1 when the instance-ID file is absent, 2 on
bad usage.
"""

import sys

from .model.instance import instance_id_path


def main() -> None:
    """Print the instance ID to stdout."""
    if len(sys.argv) != 1:
        print("usage: klangk-instance-id", file=sys.stderr)
        raise SystemExit(2)

    path = instance_id_path()
    if not path.exists():
        print(
            f"klangk-instance-id: {path} does not exist "
            "(klangkd has not written it yet)",
            file=sys.stderr,
        )
        raise SystemExit(1)
    sys.stdout.write(path.read_text().strip())


if __name__ == "__main__":  # pragma: no cover
    main()
