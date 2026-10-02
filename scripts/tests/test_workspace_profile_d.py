"""Contract tests for the workspace profile.d PATH snippet (#3522).

``profile.d/klangk-path.sh`` re-prepends ``/opt/klangk/bin`` for every
login shell (issue #1093 documents why this lives in profile.d, not
bash.bashrc) and, since #3522, ``$HOME/.local/bin`` in front of it:
executables a user installs into the user bin directory (``uv tool
install`` and friends) must resolve on the next shell and must shadow
the vendored klangk-* helpers.

Behavioral tests, not grep-style: the snippet is sourced by a real POSIX
``sh`` with controlled ``PATH``/``HOME``, mirroring how ``/etc/profile``
sources it via run-parts (under dash). On the dev host ``sh`` is
bash-as-sh; ``SHELLS`` adds ``dash`` where available (CI's ubuntu
runners have it), so the quoting-sensitive case pattern gets exercised
under both.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

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
LOCAL_FIRST = f"/home/klangk/.local/bin:/opt/klangk/bin:{BASE_PATH}"
KLANGK_ONLY = f"/opt/klangk/bin:{BASE_PATH}"
SHELLS = ["sh"] + (["dash"] if shutil.which("dash") else [])

# Debian's skel ~/.profile conditional, sourced AFTER /etc/profile's
# profile.d loop in a real login shell: it re-prepends ~/.local/bin once
# the directory exists. The snippet must tolerate that (duplicate entry,
# ~/.local/bin still first).
SKEL_BLOCK = (
    'if [ -d "$HOME/.local/bin" ]; then\n'
    '  PATH="$HOME/.local/bin:$PATH"\n'
    "fi\n"
    "export PATH"
)


def source_snippet(
    path_env: str,
    home: str | None,
    again: bool = False,
    epilogue: str = "",
    shell: str = "sh",
) -> str:
    """Source the snippet under a POSIX shell; return the resulting PATH."""
    lines = [f'PATH="{path_env}"', "export PATH"]
    if home is None:
        lines.append("unset HOME")
    else:
        lines += [f'HOME="{home}"', "export HOME"]
    lines.append(f'. "{SNIPPET}"')
    if again:
        lines.append(f'. "{SNIPPET}"')
    if epilogue:
        lines.append(epilogue)
    lines.append('printf "%s" "$PATH"')
    proc = subprocess.run(
        [shell, "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_local_bin_prepended_in_front_of_klangk_bin() -> None:
    assert source_snippet(BASE_PATH, "/home/klangk") == LOCAL_FIRST


def test_sourcing_twice_is_idempotent() -> None:
    assert source_snippet(BASE_PATH, "/home/klangk", again=True) == LOCAL_FIRST


def test_entry_already_on_path_keeps_its_position() -> None:
    # PATH inherited with BOTH entries present (a nested login shell
    # before /etc/profile's reset, or an interactive shell spawned from
    # one): the snippet must not duplicate or move either entry.
    path_env = f"/home/klangk/.local/bin:/opt/klangk/bin:{BASE_PATH}"
    assert source_snippet(path_env, "/home/klangk") == path_env


def test_local_only_path_gets_klangk_prepended() -> None:
    # Known composition limit, unreachable in a container login shell
    # (/etc/profile resets PATH, and the image ENV carries
    # /opt/klangk/bin): a PATH that somehow has ~/.local/bin but not
    # /opt/klangk/bin ends with /opt/klangk/bin in front.
    path_env = f"/home/klangk/.local/bin:{BASE_PATH}"
    assert source_snippet(path_env, "/home/klangk") == (f"/opt/klangk/bin:{path_env}")


def test_home_unset_or_empty_leaves_only_klangk_prefix() -> None:
    assert source_snippet(BASE_PATH, None) == KLANGK_ONLY
    assert source_snippet(BASE_PATH, "") == KLANGK_ONLY


def test_home_with_spaces() -> None:
    home = "/home/alice von neumann"
    assert source_snippet(BASE_PATH, home) == (
        f"{home}/.local/bin:/opt/klangk/bin:{BASE_PATH}"
    )


def test_debian_skel_profile_reprepend_keeps_local_bin_first(
    tmp_path: os.PathLike[str],
) -> None:
    home = os.path.join(str(tmp_path), "home")
    os.makedirs(os.path.join(home, ".local", "bin"))
    result = source_snippet(BASE_PATH, home, epilogue=SKEL_BLOCK)
    assert result == f"{home}/.local/bin:{home}/.local/bin:/opt/klangk/bin:{BASE_PATH}"


@pytest.mark.parametrize("shell", SHELLS)
def test_matrix_under_each_available_posix_shell(shell: str) -> None:
    assert source_snippet(BASE_PATH, "/home/klangk", shell=shell) == LOCAL_FIRST
    assert (
        source_snippet(BASE_PATH, "/home/klangk", again=True, shell=shell)
        == LOCAL_FIRST
    )
    assert source_snippet(BASE_PATH, None, shell=shell) == KLANGK_ONLY
    assert source_snippet(BASE_PATH, "", shell=shell) == KLANGK_ONLY
