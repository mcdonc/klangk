"""Contract tests for the workspace profile.d PATH snippet (#3522).

``profile.d/klangk-path.sh`` re-prepends ``/opt/klangk/bin`` for every
login shell (issue #1093 documents why this lives in profile.d, not
bash.bashrc) and, since #3522, ``$HOME/.local/bin`` in front of it:
executables a user installs with ``uv tool install``, ``pipx``, or
``pip install --user`` must resolve on the next shell and must shadow
the vendored klangk-* helpers.

Behavioral tests, not grep-style: the snippet is sourced by a real POSIX
``sh`` with controlled ``PATH``/``HOME``, mirroring how ``/etc/profile``
sources it via run-parts (under dash), so ordering, idempotency, and the
missing-HOME guard are exercised for real.
"""

from __future__ import annotations

import os
import subprocess

SNIPPET = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "src",
    "containers",
    "workspace",
    "profile.d",
    "klangk-path.sh",
)

BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


def source_snippet(path_env: str, home: str | None, again: bool = False) -> str:
    """Source the snippet under ``sh`` and return the resulting PATH."""
    lines = [f'PATH="{path_env}"', "export PATH"]
    if home is None:
        lines.append("unset HOME")
    else:
        lines += [f'HOME="{home}"', "export HOME"]
    lines.append(f'. "{SNIPPET}"')
    if again:
        lines.append(f'. "{SNIPPET}"')
    lines.append('printf "%s" "$PATH"')
    proc = subprocess.run(
        ["sh", "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_local_bin_prepended_in_front_of_klangk_bin() -> None:
    assert source_snippet(BASE_PATH, "/home/klangk") == (
        f"/home/klangk/.local/bin:/opt/klangk/bin:{BASE_PATH}"
    )


def test_sourcing_twice_is_idempotent() -> None:
    twice = source_snippet(BASE_PATH, "/home/klangk", again=True)
    assert twice == f"/home/klangk/.local/bin:/opt/klangk/bin:{BASE_PATH}"


def test_entry_already_on_path_keeps_its_position() -> None:
    # Debian's skel ~/.profile prepends ~/.local/bin itself when the
    # directory exists: the snippet must not duplicate or move the entry.
    path_env = f"/opt/klangk/bin:/home/klangk/.local/bin:{BASE_PATH}"
    assert source_snippet(path_env, "/home/klangk") == path_env


def test_home_unset_leaves_only_klangk_prefix() -> None:
    assert source_snippet(BASE_PATH, None) == f"/opt/klangk/bin:{BASE_PATH}"
