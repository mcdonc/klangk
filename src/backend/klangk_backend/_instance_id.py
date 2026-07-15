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

import os
import sys

from .settings import KlangkSettings
from .util import Util


class _ShimAppState:
    """Minimal stand-in ``app.state`` for the instance-id shim.

    The real ``Util`` reads ``app_state.settings``; that's all the shim
    needs (it never touches the DB, podman, or anything else on app.state).
    Avoids constructing a full app state — or a module-level global — for a
    process whose only job is to print one file's contents.
    """

    __slots__ = ("settings",)

    def __init__(self, settings: KlangkSettings):
        self.settings = settings


def main() -> None:
    """Print the instance ID to stdout."""
    if len(sys.argv) != 1:
        print("usage: klangk-instance-id", file=sys.stderr)
        raise SystemExit(2)

    # External process: no app_state. Build a Util from env-derived settings
    # just to reuse the same path resolution (<data_dir>/instance-id) the
    # server uses. We never call instance_id()/resolve_instance_id() here —
    # the shim is read-only and never writes the file.
    util = Util(_ShimAppState(KlangkSettings(os.environ)))
    path = util.instance_id_path()
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
