"""Contract tests for the workspace container's tmux.conf (#2694).

Selection auto-copy in ``klangk shell`` depends on three tmux.conf lines
working together; any one of them silently dropped breaks a clipboard path
with no test failure elsewhere:

1. ``set -g set-clipboard on`` — makes the container tmux itself emit the
   OSC 52 (clipboard set) escape to its attached client when a copy-mode
   selection is made. The attached client is the server-side
   ``podman exec tmux attach`` whose pty output is forwarded over the
   WebSocket to the CLI, so this is the ONLY working OSC 52 transport for
   CLI shells: the copy-command helper (klangk-copy-to-clipboard) runs
   without a controlling terminal, so its own /dev/tty OSC 52 write is a
   no-op (#2694's root cause).
2. ``set -ga terminal-features ",xterm-256color:clipboard"`` — terminal
   attaches always run with TERM=xterm-256color (terminal.build_environment),
   and stock xterm-256color terminfo has no Ms capability; without the
   feature tmux believes the client can't do OSC 52 and drops it silently.
   (The feature form, not an Ms terminal-override, because the container's
   tmux 3.5a mangles escape sequences parsed from the config file.)
3. ``set -g copy-command "klangk-copy-to-clipboard"`` — the browser path:
   copy-pipe pipes the selection to the helper, which POSTs it to the
   browser-delegate bridge so the tab's clipboard is set.

Grep-style contract tests in the spirit of test_podman_registries_conf.py:
a future edit that drops one silently is loud.
"""

from __future__ import annotations

import os
import re

import pytest

TMUX_CONF = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "src",
    "containers",
    "workspace",
    "tmux.conf",
)


@pytest.fixture(scope="module")
def conf() -> str:
    with open(TMUX_CONF) as f:
        return f.read()


def test_set_clipboard_on(conf: str) -> None:
    """Copies must emit OSC 52 to attached clients (#2694)."""
    assert re.search(r"^set -g set-clipboard on$", conf, re.M), (
        "set-clipboard on dropped: klangk shell selections would stop "
        "reaching the local clipboard via OSC 52"
    )


def test_clipboard_feature_for_attach_term(conf: str) -> None:
    """The xterm-256color clipboard feature must claim OSC 52 support.

    Terminal attaches hardcode TERM=xterm-256color; without the feature
    tmux silently drops the OSC 52 write even with set-clipboard on.
    """
    assert (
        'set -ga terminal-features ",xterm-256color:clipboard"' in conf.splitlines()
    ), "clipboard feature dropped: OSC 52 writes would be dropped"


def test_copy_command_bridge_intact(conf: str) -> None:
    """The browser bridge path (copy-pipe -> helper) must stay wired."""
    assert re.search(r'^set -g copy-command "klangk-copy-to-clipboard"$', conf, re.M), (
        "copy-command dropped: browser selections would stop auto-copying"
    )


def test_copy_bindings_still_copy_pipe(conf: str) -> None:
    """Drag/Enter/y copies must pipe through copy-command (browser path)."""
    for table in ("copy-mode", "copy-mode-vi"):
        assert re.search(
            rf"bind -T {table} (?:MouseDragEnd1Pane|Enter|y) "
            rf"send-keys -X copy-pipe-and-cancel",
            conf,
        ), f"{table} copy-pipe binding dropped"
