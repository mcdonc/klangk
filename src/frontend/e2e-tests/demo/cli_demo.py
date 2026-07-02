#!/usr/bin/env python3
"""Expect-style driver for scripted CLI demo scenes (records to 1080p via
``record-terminal.sh``).

This is the "pyexpect" the intro-video plan called for — a Python driver that
drives a terminal the way ``pexpect`` drives a headless pty — implemented on
``tmux send-keys`` / ``capture-pane`` instead of on a raw pty.

Why not pexpect directly?
    ``pexpect.spawn()`` takes the **master** side of the pty it creates, so it
    can read/write the child process — but then **no terminal emulator can
    render that session live**, because a terminal emulator must itself be the
    pty master. For a *displayed* terminal recording you need a layer that
    multiplexes the pty: one client renders it (xterm, on the Xvfb display
    that ffmpeg captures) while another scripts it. ``tmux`` is exactly that
    layer, and its ``send-keys``/``capture-pane`` pair gives the same
    ``send``/``expect`` primitives pexpect provides — without taking the pty
    hostage. (Verified empirically; see the README.) The driver below is
    therefore **stdlib-only**: no ``pexpect``, no ``pip install``, runs on any
    ``python3``.

Scenes
    Each scene is a function ``scene_<name>(t: Term)``. The driver calls
    ``tmux send-keys``/``capture-pane`` against ``$KLANGK_DEMO_TMUX_SESSION``
    (set by ``record-terminal.sh``). Run a scene with ``--scene <name>``.

    The built-in ``demo`` scene needs **no klangk server** — it's a
    self-contained smoke test so the recorder is verifiable anywhere. The
    ``scene_2`` / ``scene_3`` / ``scene_4`` stubs drive the real ``klangkc``
    CLI and require a live server (see ``README.md``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import Callable


class TimeoutError_(Exception):
    """Raised when ``expect()`` does not see its text in time."""


class Term:
    """Drive a tmux session with expect-style primitives.

    Wraps ``tmux send-keys`` (write) and ``tmux capture-pane`` (read) against a
    named session. Designed to be driven by ``record-terminal.sh``, which
    creates the session, attaches xterm to it, and records the Xvfb display.
    """

    def __init__(
        self,
        session: str | None = None,
        *,
        typewriter: float = 0.0,
        key_delay: float = 0.0,
    ) -> None:
        self.session = session or os.environ.get(
            "KLANGK_DEMO_TMUX_SESSION", "klangk-demo"
        )
        # typewriter: per-character delay when type()-ing (a "live typing"
        #   look reads much better on camera than an instant paste).
        # key_delay: pause after each Enter (lets output land before the next
        #   command, avoids a blurred machine-gun look).
        self.typewriter = typewriter
        self.key_delay = key_delay

    # -- low level ----------------------------------------------------------
    def _send(self, text: str) -> None:
        # -l = literal: do not interpret text as tmux key names (so '$', ';',
        # etc. are typed as-is).
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session, "-l", text],
            check=True,
        )

    def _enter(self) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.session, "Enter"], check=True)

    def pane(self, *, lines: int = 50) -> str:
        """Current visible pane contents (for ``expect`` matching)."""
        res = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                self.session,
                "-p",
                "-S",
                f"-{lines}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout

    # -- high level (the "expect" API) -------------------------------------
    def type(self, text: str, *, per_char: float | None = None) -> None:
        """Type text, optionally one character at a time (typewriter effect)."""
        delay = self.typewriter if per_char is None else per_char
        if delay <= 0:
            self._send(text)
            return
        for ch in text:
            self._send(ch)
            time.sleep(delay)

    def enter(self) -> None:
        self._enter()
        if self.key_delay:
            time.sleep(self.key_delay)

    def run(
        self,
        cmd: str,
        *,
        expect: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        """Type a command, press Enter, optionally wait for ``expect`` text."""
        self.type(cmd)
        self.enter()
        if expect is not None:
            return self.expect(expect, timeout=timeout)
        return ""

    def expect(
        self,
        text: str,
        *,
        timeout: float = 30.0,
        poll: float = 0.15,
    ) -> str:
        """Block until ``text`` appears in the pane; return the pane text."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pane = self.pane()
            if text in pane:
                return pane
            time.sleep(poll)
        raise TimeoutError_(
            f"timed out after {timeout:.0f}s waiting for {text!r} in tmux "
            f"session {self.session!r}"
        )

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def clear(self) -> None:
        self.run("clear")

    # -- multi-pane support --------------------------------------------
    # These target the *active* pane of the session. split() makes the new pane
    # active; select_pane() switches the active pane back. The other methods
    # (type/enter/run/expect/pane) always hit the active pane, so a split plus a
    # later select_pane is all that's needed to drive two panes from one Term.
    def pane_id(self) -> str:
        """Return the tmux pane id (e.g. ``%3``) of the active pane."""
        res = subprocess.run(
            [
                "tmux",
                "display-message",
                "-t",
                self.session,
                "-p",
                "#{pane_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()

    def split(self) -> str:
        """Split the window side-by-side; return the new pane's id.

        The new pane runs the same rcfile shell as the session (venv PATH, host
        prompt, steady cursor — see ``record-terminal.sh``), so it looks
        identical to the first pane and becomes the active pane. The split is a
        tmux control call, so it never appears as typed text in the recording.
        """
        rcfile = os.environ.get("KLANGK_DEMO_RCFILE", "")
        cmd = ["bash"]
        if rcfile:
            cmd += ["--rcfile", rcfile]
        cmd += ["-i"]
        res = subprocess.run(
            [
                "tmux",
                "split-window",
                "-h",
                "-t",
                self.session,
                "-P",
                "-F",
                "#{pane_id}",
                *cmd,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()

    def select_pane(self, pane_id: str) -> None:
        """Make *pane_id* the active pane."""
        subprocess.run(["tmux", "select-pane", "-t", pane_id], check=True)

    def kill_pane(self, pane_id: str) -> None:
        """Close *pane_id*; the remaining pane resizes to fill the space."""
        subprocess.run(["tmux", "kill-pane", "-t", pane_id], check=True)


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------


def _intro(t: Term, title: str) -> None:
    """A small on-screen title card (pure ANSI, no server needed)."""
    t.run(
        'printf "\\033[2J\\033[H"',  # clear + home
        expect="$",
    )
    t.run(
        f'printf "\\033[1;36m# {title}\\033[0m\\n"',
        expect="$",
    )
    t.pause(0.8)


def scene_demo(t: Term) -> None:
    """Self-contained scene — no klangk server required.

    Exercises everything the recorder needs to prove: typing, multi-line
    output, color, a short loop with sleeps, and a clean ending. Use it as the
    smoke test for the recorder and as a copy-paste template for real scenes.
    """
    _intro(t, "Klangk demo recorder — smoke test")

    t.run('echo "This terminal is being scripted and recorded at 1080p."')
    t.pause(0.6)

    # Typewriter effect for a command that "reads" well on camera.
    t.type("for i in $(seq 1 3)", per_char=0.03)
    t.enter()
    t.type(
        "  do printf '\\033[33mline %d: hello world\\033[0m\\n' \"$i\"", per_char=0.02
    )
    t.enter()
    t.type("  sleep 0.5", per_char=0.03)
    t.enter()
    t.type("done", per_char=0.03)
    t.enter()
    t.expect("line 3: hello world", timeout=10)
    t.pause(0.8)

    t.run('printf "\n\033[32m\u2713 done\033[0m — edit this in DaVinci Resolve.\n"')
    t.pause(1.2)


def _wait_remote(t: Term, *, timeout: float = 120.0) -> None:
    """Wait until the remote container prompt is idle.

    A remote prompt starts with ``~`` and ends with ``$`` (e.g. ``~$``,
    ``~/klangk$``), so this is directory-agnostic. We wait until the last
    non-empty pane line IS such a prompt — which means the command we just ran
    has finished and the shell is ready again. The ``Connecting`` banner from
    ``klangkc shell`` is excluded so we don't match mid-handshake.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = t.pane()
        if "Connecting" in pane:
            time.sleep(0.5)
            continue
        lines = [ln for ln in pane.splitlines() if ln.strip()]
        if lines:
            last = lines[-1].rstrip()
            if last.startswith("~") and last.endswith("$"):
                return
        time.sleep(0.5)
    raise TimeoutError_(f"remote prompt did not settle within {timeout:.0f}s")


def _disconnect_ssh(t: Term, *, hold: float = 0.0) -> None:
    """Send the SSH-style ``~.`` escape to the active pane, wait for the host
    prompt, then optionally pause.

    ``~.`` only fires right after a fresh newline, so it is sent as three
    separate writes (Enter, then ``~``, then ``.``). Deduplicated here because
    scene_2 disconnects several times.
    """
    t.enter()  # newline first
    t._send("~")  # tilde at start of line
    t.pause(0.3)
    t._send(".")  # dot
    t.pause(1.0)
    t.expect("host $", timeout=10)
    if hold:
        t.pause(hold)


def scene_2(t: Term) -> None:
    """CLI: Creating Your First Workspace (~2 min).

    The hero user is ``admin@example.com`` (created once via the admin API).
    This scene creates the ``demo`` workspace, which is KEPT for continuity —
    the browser scenes later operate on the same workspace. Requires a live
    klangk server reachable by ``klangkc`` (see README).
    """
    server = os.environ.get("KLANGK_DEMO_SERVER", "http://localhost:8995")
    admin = os.environ.get("KLANGK_DEMO_USER", "admin@example.com")
    password = os.environ.get("KLANGK_DEMO_PASSWORD", "adminpass")
    # Hold between commands so the voiceover has room to breathe.
    HOLD = float(os.environ.get("KLANGK_DEMO_HOLD", "5"))

    # --- login --------------------------------------------------------
    # No narration is printed — the voiceover covers it. klangkc login prompts
    # for a password (masked, no echo).
    t.type(f"klangkc login {server} {admin}", per_char=0.03)
    t.enter()
    t.expect("Password", timeout=15)
    t.type(password, per_char=0.06)  # masked: no echo, but type at demo speed
    t.enter()
    t.expect("Logged in", timeout=15)
    t.pause(HOLD)

    # --- create -------------------------------------------------------
    t.type("klangkc create demo", per_char=0.03)
    t.enter()
    t.expect("Created workspace", timeout=30)
    t.pause(HOLD)

    # --- FIRST shell: drop into the container (plain) ----------------
    # Demonstrate the container experience, then reconnect with -A to clone.
    t.type("klangkc shell demo", per_char=0.03)
    t.enter()
    t.expect("Escape: Enter", timeout=15)  # the "~." hint line
    _wait_remote(t, timeout=20)
    t.pause(HOLD)

    # hostname prints the container ID — a clear cue that we're in a fresh
    # container. (whoami would return "klangk", confusing next to the
    # admin@example.com login, so we skip it.)
    t.run("hostname", timeout=10)
    _wait_remote(t, timeout=10)
    t.pause(HOLD)

    # --- disconnect (~.) to demonstrate reconnect/persistence ----------
    _disconnect_ssh(t, hold=HOLD)

    # --- SECOND shell: re-enter with agent forwarding (-A) -------------
    t.type("klangkc shell demo -A", per_char=0.03)
    t.enter()
    t.expect("Escape: Enter", timeout=15)
    _wait_remote(t, timeout=20)
    t.pause(HOLD)

    # ssh-add -l proves the agent was forwarded — the host keys appear here,
    # even though no private key was ever copied into the container.
    t.run("ssh-add -l", timeout=10)
    _wait_remote(t, timeout=10)
    t.pause(HOLD)

    # --- clone a repo over SSH (proves the forwarded keys work) --------
    t.type("git clone git@github.com:mcdonc/klangk.git", per_char=0.03)
    t.enter()
    t.expect("Cloning into", timeout=15)
    _wait_remote(t, timeout=60)
    t.pause(HOLD)

    # --- run pi to audit the codebase ---------------------------------
    t.run("cd klangk", timeout=10)
    _wait_remote(t, timeout=10)
    t.pause(HOLD)
    t.type(
        "pi -p 'In two sentences, what does this codebase do?'",
        per_char=0.03,
    )
    t.enter()
    # pi -p talks to the klangk LLM proxy and takes ~20-30s to respond.
    _wait_remote(t, timeout=120)
    t.pause(HOLD)

    # --- split-pane beat: a second CLI window into the same workspace ---
    # The split is a tmux control call — it never appears as typed text in the
    # recording. The new pane runs the same rcfile shell (see
    # record-terminal.sh), so it looks identical to the first pane and becomes
    # the active pane.
    orig = t.pane_id()
    new = t.split()
    t.pause(HOLD)

    # Connect to a NAMED window ("logs") — a separate terminal in the same
    # workspace (created if absent), not the active one. Shows that one
    # workspace can have several CLI terminals open at once.
    t.type("klangkc shell demo logs", per_char=0.03)
    t.enter()
    t.expect("Escape: Enter", timeout=15)
    _wait_remote(t, timeout=20)
    t.pause(HOLD)

    # `ls` proves it's the same workspace: the cloned `klangk` dir is right
    # there in the home directory.
    t.run("ls", timeout=10)
    _wait_remote(t, timeout=10)
    t.pause(HOLD)

    # Disconnect the second pane (~.), then close it; the original pane
    # resizes back to full width for the finale.
    _disconnect_ssh(t, hold=HOLD)
    t.kill_pane(new)
    t.select_pane(orig)
    t.pause(0.5)

    # Disconnect the first pane (~.).
    _disconnect_ssh(t, hold=HOLD)

    # --- list workspaces from the host ---------------------------------
    # klangkc ls is a quick API call; `demo` (and any seeded workspaces) show.
    # No `expect` here: any plausible marker ("demo", the "Name" header) would
    # false-match scrollback (e.g. the earlier `klangkc shell demo` line), so
    # a fixed pause is safer for this final beat.
    t.type("klangkc ls", per_char=0.03)
    t.enter()
    t.pause(3)
    t.pause(HOLD)
    # The workspace is KEPT for the browser scenes (continuity) — do NOT rm.


def scene_3(t: Term) -> None:
    """klangkc sandbox: one command to rule them all (~1.5 min).

    Requires a live server. Skeleton — point at a real project dir.
    """
    _intro(t, "klangkc sandbox — one command")
    t.run("cd ~/projects/myproject", expect="$")
    t.run("klangkc sandbox myproject -A", expect="$", timeout=120)
    t.pause(1.0)


def scene_4(t: Term) -> None:
    """openclaw service sandbox (~1.5 min). Requires a live server."""
    _intro(t, "A long-lived service: openclaw")
    t.run("cd sandboxes/openclaw", expect="$")
    t.run("klangkc sandbox openclaw", expect="$", timeout=180)
    t.pause(1.0)
    t.run("klangkc monitor --type service_health | jq .", expect="$", timeout=20)
    t.pause(1.0)


SCENES: dict[str, Callable[[Term], None]] = {
    "demo": scene_demo,
    "scene_2": scene_2,
    "scene_3": scene_3,
    "scene_4": scene_4,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Drive a scripted CLI terminal scene (for record-terminal.sh)."
    )
    ap.add_argument(
        "--scene",
        required=True,
        choices=sorted(SCENES),
        help="scene to run (use 'demo' for a no-server smoke test)",
    )
    ap.add_argument(
        "--session",
        default=os.environ.get("KLANGK_DEMO_TMUX_SESSION", "klangk-demo"),
        help="tmux session to drive (default: $KLANGK_DEMO_TMUX_SESSION)",
    )
    ap.add_argument(
        "--typewriter",
        type=float,
        default=float(os.environ.get("KLANGK_DEMO_TYPEWRITER", "0")),
        help="per-character delay (s) for the typewriter effect",
    )
    ap.add_argument(
        "--key-delay",
        type=float,
        default=float(os.environ.get("KLANGK_DEMO_KEY_DELAY", "0.4")),
        help="pause (s) after each Enter",
    )
    args = ap.parse_args(argv)

    if not shutil.which("tmux"):
        print("error: tmux not found on PATH", file=sys.stderr)
        return 2

    t = Term(
        args.session,
        typewriter=args.typewriter,
        key_delay=args.key_delay,
    )
    print(
        f"=== driving scene {args.scene!r} on tmux session {args.session!r} ===",
        flush=True,
    )
    try:
        SCENES[args.scene](t)
    except TimeoutError_ as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("=== scene complete ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
