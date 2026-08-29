"""Shared helpers for the consent CLI scripts (consent-watch, consent-decide)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir(arg: str | None) -> Path:
    """The klangkd data dir: the --data-dir argument, else $KLANGKD_DATA_DIR;
    exits with guidance when neither is set."""
    path = arg or os.environ.get("KLANGKD_DATA_DIR")
    if not path:
        sys.exit(
            "data dir not set: pass --data-dir or export KLANGKD_DATA_DIR "
            "(the klangkd dir containing klangk.db)"
        )
    return Path(path)
