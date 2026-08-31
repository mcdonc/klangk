"""Tests for the client-side consent-popup shell wrapper (#2383).

Covers the pure tmux command builders, the gate, and the orchestrator's
command sequence (with the tmux runner / attach injected as recorders).
"""

from __future__ import annotations

import re
import shlex
from unittest.mock import MagicMock, patch

import pytest


from klangk.cli import shell_popup as sp


# ---------------------------------------------------------------------------
# version parsing + detection
# ---------------------------------------------------------------------------


class TestTmuxVersion:
    def test_parse_typical(self):
        assert sp.parse_tmux_version("tmux 3.6a") == (3, 6)

    def test_parse_no_suffix(self):
        assert sp.parse_tmux_version("tmux 3.2") == (3, 2)

    def test_parse_garbage(self):
        assert sp.parse_tmux_version("nope") is None

    def test_parse_empty(self):
        assert sp.parse_tmux_version("") is None

    def test_min_version_is_3_2(self):
        assert sp.TMUX_MIN_VERSION == (3, 2)

    def test_usable_new_enough(self):
        assert sp.tmux_usable((3, 6)) is True
        assert sp.tmux_usable((3, 2)) is True

    def test_usable_old(self):
        assert sp.tmux_usable((3, 1)) is False

    def test_usable_absent(self):
        assert sp.tmux_usable(None) is False

    def test_host_version_parses(self):
        with (
            patch(
                "klangk.cli.shell_popup.shutil.which",
                return_value="/usr/bin/tmux",
            ),
            patch(
                "klangk.cli.shell_popup.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="tmux 3.6a"),
            ),
        ):
            assert sp.host_tmux_version() == (3, 6)

    def test_host_version_absent(self):
        with patch("klangk.cli.shell_popup.shutil.which", return_value=None):
            assert sp.host_tmux_version() is None

    def test_host_version_run_fails(self):
        with (
            patch(
                "klangk.cli.shell_popup.shutil.which",
                return_value="/usr/bin/tmux",
            ),
            patch(
                "klangk.cli.shell_popup.subprocess.run",
                return_value=MagicMock(returncode=1, stdout=""),
            ),
        ):
            assert sp.host_tmux_version() is None

    def test_host_version_oserror(self):
        with (
            patch(
                "klangk.cli.shell_popup.shutil.which",
                return_value="/usr/bin/tmux",
            ),
            patch(
                "klangk.cli.shell_popup.subprocess.run",
                side_effect=OSError("boom"),
            ),
        ):
            assert sp.host_tmux_version() is None


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------


class TestNaming:
    def test_socket_path_stable_and_sanitized(self):
        a = sp.socket_path("wsid")
        b = sp.socket_path("wsid")
        assert a == b
        assert a.endswith("wsid.sock")

    def test_socket_path_no_dot_or_colon(self):
        # tmux session names / sockets can't contain '.' or ':'; ids with them
        # must be sanitized so the session name is valid.
        p = sp.socket_path("w.s:i")
        assert ":" not in p and "w.s:i.sock" not in p

    def test_session_names_prefixed_and_safe(self):
        outer = sp.outer_session_name("wsid")
        hidden = sp.hidden_session_name("wsid")
        assert outer == "klangk-shell-wsid"
        assert hidden == "klangk-consent-wsid"
        # No tmux-illegal chars even for hostile input.
        bad = sp.outer_session_name("a.b:c")
        assert "." not in bad and ":" not in bad

    def test_popup_session_names_unique_per_invocation(self):
        """Each invocation gets its own (outer, hidden) pair so a second
        concurrent shell into the same workspace cannot collide with (or
        attach to) the first shell's sessions (#2692)."""
        a = sp.popup_session_names("wsid")
        b = sp.popup_session_names("wsid")
        assert re.match(r"klangk-shell-wsid-p\d+-[0-9a-f]+$", a[0])
        assert re.match(r"klangk-consent-wsid-p\d+-[0-9a-f]+$", a[1])
        # Unique across invocations...
        assert a != b
        # ...and the pair shares one suffix (wrapper + decider agree).
        assert a[0].rsplit("-", 2)[-2:] == a[1].rsplit("-", 2)[-2:]


# ---------------------------------------------------------------------------
# pure command builders
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_new_detached_session(self):
        cmd = sp.new_detached_session(
            "/tmp/s.sock", "sess", ["bash", "-l"], x=80, y=24
        )
        assert cmd == [
            "tmux",
            "-S",
            "/tmp/s.sock",
            "new-session",
            "-d",
            "-s",
            "sess",
            "-x",
            "80",
            "-y",
            "24",
            "bash",
            "-l",
        ]

    def test_configure_outer_session(self):
        cmds = sp.configure_outer_session("/tmp/s.sock", "outer")
        assert cmds == [
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-t",
                "outer",
                "prefix",
                "C-a",
            ],
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-t",
                "outer",
                "status",
                "off",
            ],
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-t",
                "outer",
                "mouse",
                "off",
            ],
            # Clipboard forwarding (#2694): re-emit the inner shell's OSC 52
            # (clipboard set) to the real terminal, and claim the clipboard
            # feature for every TERM (stock terminfo lacks Ms).
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-g",
                "set-clipboard",
                "on",
            ],
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-ga",
                "terminal-features",
                ",*:clipboard",
            ],
        ]

    def test_configure_hidden_session(self):
        # Hide the hidden session's status bar so the popup shows only the
        # decider (no tmux status bar across the popup's bottom).
        assert sp.configure_hidden_session("/tmp/s.sock", "hidden") == [
            [
                "tmux",
                "-S",
                "/tmp/s.sock",
                "set-option",
                "-t",
                "hidden",
                "status",
                "off",
            ],
        ]

    def test_popup_viewer_shell_string(self):
        assert sp.popup_viewer_shell_string("/tmp/s.sock", "hidden") == (
            "env -u TMUX tmux -S /tmp/s.sock attach -t hidden"
        )

    def test_display_popup_command(self):
        cmd = sp.display_popup_command("/tmp/s.sock", "hidden", w=70, h=14)
        assert cmd.startswith("display-popup -E ")
        assert "env -u TMUX tmux -S /tmp/s.sock attach -t hidden" in cmd
        assert "-w 70 -h 14" in cmd
        # The shell-command MUST be the final positional (options first) or
        # tmux swallows the trailing -w/-h into the command and the popup
        # renders blank (#2383).
        assert cmd.endswith(
            shlex.quote(sp.popup_viewer_shell_string("/tmp/s.sock", "hidden"))
        )

    def test_popup_binding_cmds(self):
        # Just the <prefix> reopen binding — no auto-show hook (the decider
        # shows the popup itself when a request arrives).
        cmds = sp.popup_binding_cmds("/tmp/s.sock", "hidden", w=70, h=14)
        assert len(cmds) == 1
        assert cmds[0][:4] == ["tmux", "-S", "/tmp/s.sock", "bind-key"]
        assert cmds[0][4] == sp.REOPEN_KEY
        assert "display-popup" in cmds[0][5]

    def test_show_popup_argv(self):
        argv = sp.show_popup_argv(
            "/tmp/s.sock", "hidden", "clientA", w=70, h=14
        )
        assert argv == [
            "tmux",
            "-S",
            "/tmp/s.sock",
            "display-popup",
            "-c",
            "clientA",
            "-E",
            "-w",
            "70",
            "-h",
            "14",
            "env -u TMUX tmux -S /tmp/s.sock attach -t hidden",
        ]

    def test_outer_clients_excludes_hidden(self):
        clients = "myclient\touter\nviewer\thidden\nother\touter\n"
        with patch(
            "klangk.cli.shell_popup.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=clients),
        ):
            assert sp.outer_clients("/tmp/s.sock", "hidden") == [
                "myclient",
                "other",
            ]

    def test_outer_clients_empty_on_error(self):
        with patch(
            "klangk.cli.shell_popup.subprocess.run", side_effect=OSError("x")
        ):
            assert sp.outer_clients("/tmp/s.sock", "hidden") == []

    def test_hidden_has_client(self):
        with patch(
            "klangk.cli.shell_popup.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="clientA\n"),
        ):
            assert sp.hidden_has_client("/tmp/s.sock", "hidden") is True
        with patch(
            "klangk.cli.shell_popup.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=""),
        ):
            assert sp.hidden_has_client("/tmp/s.sock", "hidden") is False

    def test_hidden_has_client_false_on_error(self):
        with patch(
            "klangk.cli.shell_popup.subprocess.run", side_effect=OSError("x")
        ):
            assert sp.hidden_has_client("/tmp/s.sock", "hidden") is False

    def test_attach_kill_has_session(self):
        assert sp.attach_cmd("/tmp/s.sock", "s") == [
            "tmux",
            "-S",
            "/tmp/s.sock",
            "attach",
            "-t",
            "s",
        ]
        assert sp.kill_session_cmd("/tmp/s.sock", "s") == [
            "tmux",
            "-S",
            "/tmp/s.sock",
            "kill-session",
            "-t",
            "s",
        ]
        assert sp.has_session_cmd("/tmp/s.sock", "s") == [
            "tmux",
            "-S",
            "/tmp/s.sock",
            "has-session",
            "-t",
            "s",
        ]


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


class TestShouldUsePopup:
    def test_all_conditions_met(self):
        assert sp.should_use_popup(
            "interactive", isatty=True, tmux_version=(3, 6)
        )

    def test_disabled_flag(self):
        assert not sp.should_use_popup(
            "interactive", isatty=True, tmux_version=(3, 6), enabled=False
        )

    def test_non_interactive_egress(self):
        for mode in (None, "allow", "static"):
            assert not sp.should_use_popup(
                mode, isatty=True, tmux_version=(3, 6)
            )

    def test_no_tty(self):
        assert not sp.should_use_popup(
            "interactive", isatty=False, tmux_version=(3, 6)
        )

    def test_old_tmux(self):
        assert not sp.should_use_popup(
            "interactive", isatty=True, tmux_version=(3, 1)
        )

    def test_missing_tmux(self):
        assert not sp.should_use_popup(
            "interactive", isatty=True, tmux_version=None
        )


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


class TestRunConsentShell:
    @staticmethod
    def _run(**kwargs):
        """Drive run_consent_shell with recording run/attach stubs."""
        ran: list[list[str]] = []

        def fake_run(argv, quiet=False):
            ran.append(argv)
            return 0

        def fake_attach(argv):
            ran.append(argv)
            return 0

        rc = sp.run_consent_shell(run=fake_run, attach=fake_attach, **kwargs)
        return rc, ran

    def test_command_sequence(self):
        rc, ran = self._run(
            workspace_id="wsid",
            inner_argv=["klangk", "shell", "ws", "--no-consent-popup"],
            decider_argv=["klangk", "consent-decide", "ws"],
        )
        assert rc == 0
        socket = sp.socket_path("wsid")
        # run_consent_shell generates per-invocation names (#2692) —
        # recover them from the actual new-session commands. Stale-session
        # sweeping happens in sweep_dead_sessions (its own subprocess,
        # #2693), not through `run`, so ran starts at the outer session.
        outer = ran[0][ran[0].index("-s") + 1]
        hidden = ran[6][ran[6].index("-s") + 1]
        assert re.match(r"klangk-shell-wsid-p\d+-[0-9a-f]+$", outer)
        assert re.match(r"klangk-consent-wsid-p\d+-[0-9a-f]+$", hidden)
        # 1 outer + 5 outer-config (incl. 2 clipboard opts, #2694)
        # + 1 hidden + 1 hidden-config + 1 binding + 1 attach + 2 kills
        # = 12
        assert len(ran) == 12
        # outer session created with the inner argv, then configured
        assert (
            ran[0]
            == sp.new_detached_session(
                socket,
                outer,
                ["klangk", "shell", "ws", "--no-consent-popup"],
                x=80,
                y=24,
            )
            or ran[0][3] == outer
        )  # size may differ; check the session
        assert ran[0][1:4] == ["-S", socket, "new-session"]
        assert ran[0][4] == "-d"
        # configure: prefix/status/mouse + clipboard forwarding (#2694)
        outer_cmds = sp.configure_outer_session(socket, outer)
        assert ran[1 : 1 + len(outer_cmds)] == outer_cmds
        # hidden session
        next_i = 1 + len(outer_cmds)
        assert ran[next_i][1:4] == ["-S", socket, "new-session"]
        assert ran[next_i][ran[next_i].index("-s") + 1] == hidden
        # hidden session status bar hidden (popup shows only the decider)
        assert ran[next_i + 1 : next_i + 2] == sp.configure_hidden_session(
            socket, hidden
        )
        # the reopen binding (no auto-show hook)
        assert ran[next_i + 2 : next_i + 3] == sp.popup_binding_cmds(
            socket,
            hidden,
            w=sp.DEFAULT_POPUP_SIZE[0],
            h=sp.DEFAULT_POPUP_SIZE[1],
        )
        # attach to outer
        assert ran[next_i + 3] == sp.attach_cmd(socket, outer)
        # cleanup: kill hidden then outer
        assert ran[next_i + 4] == sp.kill_session_cmd(socket, hidden)
        assert ran[next_i + 5] == sp.kill_session_cmd(socket, outer)

    def test_session_names_pair_is_shared_with_decider(self):
        """When session_names is passed, the wrapper uses exactly that pair
        — the caller's decider argv (built from the same pair) and the
        wrapper must target the same sessions (#2692)."""
        ran = []

        def fake_run(argv, quiet=False):
            ran.append(argv)
            return 0

        def fake_attach(argv):
            ran.append(argv)
            return 0

        names = (
            "klangk-shell-wsid-p1234-decafbad",
            "klangk-consent-wsid-p1234-decafbad",
        )
        sp.run_consent_shell(
            workspace_id="wsid",
            inner_argv=["x"],
            decider_argv=["y"],
            run=fake_run,
            attach=fake_attach,
            session_names=names,
        )
        socket = sp.socket_path("wsid")
        assert ran[0][ran[0].index("-s") + 1] == names[0]
        assert ran[6][ran[6].index("-s") + 1] == names[1]
        assert ran[9] == sp.attach_cmd(socket, names[0])
        assert ran[11] == sp.kill_session_cmd(socket, names[0])

    def test_returns_attach_exit_code(self):
        def fake_run(argv, quiet=False):
            return 0

        def fake_attach(argv):
            return 42

        rc = sp.run_consent_shell(
            workspace_id="wsid",
            inner_argv=["x"],
            decider_argv=["y"],
            run=fake_run,
            attach=fake_attach,
        )
        assert rc == 42

    def test_cleanup_runs_even_if_setup_fails(self):
        # A failing run (nonzero rc) must not skip cleanup.
        ran: list[list[str]] = []

        def fake_run(argv, quiet=False):
            ran.append(argv)
            return 1  # simulate a failing tmux command

        def fake_attach(argv):
            ran.append(argv)
            return 0

        sp.run_consent_shell(
            workspace_id="wsid",
            inner_argv=["x"],
            decider_argv=["y"],
            run=fake_run,
            attach=fake_attach,
        )
        # final two commands are the cleanup kills regardless; the
        # per-invocation names are recovered from the create commands (#2692)
        socket = sp.socket_path("wsid")
        hidden = ran[6][ran[6].index("-s") + 1]
        outer = ran[0][ran[0].index("-s") + 1]
        assert ran[-2] == sp.kill_session_cmd(socket, hidden)
        assert ran[-1] == sp.kill_session_cmd(socket, outer)

    def test_custom_popup_and_term_size(self):
        ran: list[list[str]] = []

        def fake_run(argv, quiet=False):
            ran.append(argv)
            return 0

        def fake_attach(argv):
            ran.append(argv)
            return 0

        sp.run_consent_shell(
            workspace_id="wsid",
            inner_argv=["x"],
            decider_argv=["y"],
            popup_size=(50, 10),
            term_size=(100, 30),
            run=fake_run,
            attach=fake_attach,
        )
        # ran starts at the outer session (sweeping is #2693's own
        # subprocess, not `run`); ran[1:6] = the 5 outer-config commands
        # (incl. 2 clipboard opts, #2694 — see the builder test).
        outer_cmd = ran[0]
        # outer session sized to the terminal
        assert outer_cmd[outer_cmd.index("-x") + 1] == "100"
        assert outer_cmd[outer_cmd.index("-y") + 1] == "30"
        hidden_cmd = ran[6]
        # hidden session sized to the popup
        assert hidden_cmd[hidden_cmd.index("-x") + 1] == "50"
        assert hidden_cmd[hidden_cmd.index("-y") + 1] == "10"
        # popup command (the reopen binding) uses the popup size
        assert "-w 50 -h 10" in ran[8][5]

    # ---------------------------------------------------------------------------
    # real-subprocess helpers (the orchestrator's defaults)
    # ---------------------------------------------------------------------------

    def test_cleanup_runs_when_attach_raises(self):
        """An exception out of attach (e.g. KeyboardInterrupt) must still
        reap this invocation's sessions — per-invocation names are never
        reused, so a leak accumulates (#2693 review)."""
        ran = []

        def fake_run(argv, quiet=False):
            ran.append((argv, quiet))
            return 0

        def boom(argv):
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            sp.run_consent_shell(
                workspace_id="wsid",
                inner_argv=["x"],
                decider_argv=["y"],
                run=fake_run,
                attach=boom,
            )
        socket = sp.socket_path("wsid")
        hidden = ran[6][0][ran[6][0].index("-s") + 1]
        outer = ran[0][0][ran[0][0].index("-s") + 1]
        assert ran[-2][0] == sp.kill_session_cmd(socket, hidden)
        assert ran[-1][0] == sp.kill_session_cmd(socket, outer)
        # cleanup kills are quiet
        assert ran[-2][1] is True and ran[-1][1] is True

    def test_setup_failures_log_at_warning_cleanup_at_debug(self, caplog):
        """Setup-step failures stay at warning (a failed outer new-session
        is the #2692 failure class — it must be visible); cleanup kills
        are quiet (#2693 review)."""
        import subprocess as sub

        proc = sub.CompletedProcess(["tmux"], 1, stderr=b"boom")
        with patch("klangk.cli.shell_popup.subprocess.run", return_value=proc):
            with caplog.at_level("WARNING", logger="klangk.cli.shell_popup"):
                assert sp.default_run(["tmux", "-S", "s", "new-session"]) == 1
                assert (
                    sp.default_run(
                        ["tmux", "-S", "s", "kill-session"], quiet=True
                    )
                    == 1
                )
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warns) == 1  # only the setup command
        assert "new-session" in warns[0].getMessage()

    def test_sweep_dead_sessions_reaps_orphans_only(self):
        """Sessions whose embedded wrapper pid is dead are reaped; a live
        wrapper's session and pid-less (foreign) sessions are left alone
        (#2693 review)."""
        import subprocess as sub

        dead_pid, live_pid, other_pid = 999998, 4242, 31337
        listing = (
            f"klangk-shell-wsid-p{dead_pid}-abc123\n"
            f"klangk-shell-wsid-p{live_pid}-def456\n"
            f"klangk-shell-wsid-p{other_pid}-7890ab\n"
            "klangk-shell-wsid-noPid-ffffff\n"
            "other-session\n"
        )
        proc = sub.CompletedProcess(["tmux"], 0, stdout=listing)
        killed = []

        def fake_run(argv, quiet=False):
            killed.append(" ".join(argv))
            return 0

        def alive(path):
            # only live_pid is alive; every other pid (incl. this process)
            # counts as dead so exactly two sessions are reaped
            return path == f"/proc/{live_pid}"

        with patch("klangk.cli.shell_popup.subprocess.run", return_value=proc):
            n = sp.sweep_dead_sessions("wsid", run=fake_run, alive=alive)

        assert n == 2  # dead_pid + other_pid
        joined = " | ".join(killed)
        assert f"p{dead_pid}" in joined
        assert f"p{other_pid}" in joined
        assert f"p{live_pid}" not in joined
        assert "noPid" not in joined  # no pid marker — foreign/legacy, skipped
        assert "other-session" not in joined

    def test_sweep_dead_sessions_handles_missing_server_and_errors(self):
        """No tmux server on the socket (rc!=0), and a failure to even run
        tmux — both are a no-op sweep, never a raise (#2693 review)."""
        import subprocess as sub

        proc = sub.CompletedProcess(["tmux"], 1, stdout="")
        with patch("klangk.cli.shell_popup.subprocess.run", return_value=proc):
            assert sp.sweep_dead_sessions("wsid") == 0
        with patch(
            "klangk.cli.shell_popup.subprocess.run", side_effect=OSError("x")
        ):
            assert sp.sweep_dead_sessions("wsid") == 0

    def test_cleanup_on_signal_runs_cleanup_on_sighup(self):
        """SIGHUP inside the block runs cleanup then kills the process —
        the window-close path must not strand the sessions (#2693
        review). Driven by invoking the installed handler directly."""
        import signal

        calls = []
        installed = {}

        def record(sig, handler):
            installed[sig] = handler

        with (
            patch("klangk.cli.shell_popup.signal.signal", side_effect=record),
            patch(
                "klangk.cli.shell_popup.os.kill",
                side_effect=lambda *a: calls.append(("kill", a)),
            ),
        ):
            with sp.cleanup_on_signal(lambda: calls.append("cleanup")):
                handler = installed.get(signal.SIGHUP)
                assert callable(handler)
                handler(signal.SIGHUP, None)
        assert calls[0] == "cleanup"
        assert calls[1][0] == "kill"
        assert calls[1][1][1] == signal.SIGHUP


class TestDefaultHelpers:
    def test_socket_path_falls_back_on_unwritable_dir(self):
        # First makedirs (shared per-user dir) fails; the pid-suffixed
        # fallback must still produce a usable socket path.
        with patch(
            "klangk.cli.shell_popup.os.makedirs",
            side_effect=[OSError("x"), None],
        ):
            p = sp.socket_path("wsid")
        assert str(sp.os.getpid()) in p

    def test_default_run_returns_returncode(self):
        with patch(
            "klangk.cli.shell_popup.subprocess.run",
            return_value=MagicMock(returncode=7),
        ):
            assert sp.default_run(["tmux"]) == 7

    def test_default_run_captures_output(self):
        """Best-effort tmux commands must never spray stderr into the
        user's terminal — the post-exit kill-session against an
        already-dead server read like a crash (#2685 follow-up)."""
        proc = MagicMock(returncode=1, stderr=b"no server running on /x.sock")
        with patch(
            "klangk.cli.shell_popup.subprocess.run", return_value=proc
        ) as fake_run:
            assert sp.default_run(["tmux"]) == 1
        kwargs = fake_run.call_args.kwargs
        assert kwargs["stdout"] is sp.subprocess.PIPE
        assert kwargs["stderr"] is sp.subprocess.PIPE

    def test_default_run_oserror_returns_1(self):
        with patch(
            "klangk.cli.shell_popup.subprocess.run",
            side_effect=OSError("boom"),
        ):
            assert sp.default_run(["tmux"]) == 1

    def test_default_attach_returns_returncode(self):
        with patch("klangk.cli.shell_popup.subprocess.call", return_value=3):
            assert sp.default_attach(["tmux"]) == 3

    def test_term_size_from_terminal(self):
        with patch(
            "klangk.cli.shell_popup.os.get_terminal_size",
            return_value=MagicMock(columns=100, lines=40),
        ):
            assert sp._term_size() == (100, 40)

    def test_term_size_fallback(self):
        with patch(
            "klangk.cli.shell_popup.os.get_terminal_size", side_effect=OSError
        ):
            assert sp._term_size() == (80, 24)


# ---------------------------------------------------------------------------
# shell() wiring (klangk.cli.main helpers)
# ---------------------------------------------------------------------------


from types import SimpleNamespace  # noqa: E402

from klangk.cli import main as cli_main  # noqa: E402


class TestShellWiring:
    def test_klangk_argv(self):
        a = cli_main.klangk_argv("shell", "ws")
        assert a[:4] == [
            cli_main.sys.executable,
            "-m",
            "klangk.cli.main",
            "shell",
        ]
        assert a[4] == "ws"

    def test_popup_inner_shell_argv_with_target_and_agent(self):
        a = cli_main.popup_inner_shell_argv("http://s", "ws", "@1", True)
        assert a[:4] == [
            cli_main.sys.executable,
            "-m",
            "klangk.cli.main",
            "--server",
        ]
        assert a[3] == "--server" and a[4] == "http://s"
        assert "shell" in a and "ws" in a and "@1" in a
        assert "--no-consent-popup" in a
        assert "--forward-agent" in a

    def test_popup_inner_shell_argv_no_target_no_agent(self):
        a = cli_main.popup_inner_shell_argv("http://s", "ws", None, False)
        assert "--no-consent-popup" in a
        assert "--no-forward-agent" in a
        assert "@1" not in a

    def test_popup_decider_argv(self):
        a = cli_main.popup_decider_argv(
            "http://s", "ws", "/tmp/x.sock", "klangk-consent-ws"
        )
        assert "consent-decide" in a and "ws" in a
        assert "--popup-socket" in a and "/tmp/x.sock" in a
        assert "--popup-session" in a and "klangk-consent-ws" in a

    def test_consent_popup_enabled_false_when_disabled(self):
        ws = SimpleNamespace(egress_mode="interactive")
        assert cli_main.consent_popup_enabled(ws, True) is False

    def test_consent_popup_enabled_false_when_not_interactive(self):
        ws = SimpleNamespace(egress_mode="allow")
        with patch("klangk.cli.main.sys.stdin.isatty", return_value=True):
            assert cli_main.consent_popup_enabled(ws, False) is False

    def test_consent_popup_enabled_false_when_no_tty(self):
        ws = SimpleNamespace(egress_mode="interactive")
        with patch("klangk.cli.main.sys.stdin.isatty", return_value=False):
            assert cli_main.consent_popup_enabled(ws, False) is False

    def test_consent_popup_enabled_true(self):
        ws = SimpleNamespace(egress_mode="interactive")
        with (
            patch("klangk.cli.main.sys.stdin.isatty", return_value=True),
            patch(
                "klangk.cli.shellcmd.host_tmux_version", return_value=(3, 6)
            ),
        ):
            assert cli_main.consent_popup_enabled(ws, False) is True

    def test_consent_popup_enabled_false_when_old_tmux(self):
        ws = SimpleNamespace(egress_mode="interactive")
        with (
            patch("klangk.cli.main.sys.stdin.isatty", return_value=True),
            patch(
                "klangk.cli.shellcmd.host_tmux_version", return_value=(3, 1)
            ),
        ):
            assert cli_main.consent_popup_enabled(ws, False) is False

    def test_run_consent_popup_builds_argv_and_returns_rc(self):
        ws = SimpleNamespace(id="wsid", name="ws")
        captured = {}

        def fake_run(**kwargs):
            captured.update(kwargs)
            return 7

        with (
            patch(
                "klangk.cli.shellcmd.run_consent_shell", side_effect=fake_run
            ),
            patch(
                "klangk.cli.context.server_url", return_value="http://server"
            ),
        ):
            rc = cli_main.run_consent_popup(ws, "@1", True)
        assert rc == 7
        assert captured["workspace_id"] == "wsid"
        assert "--no-consent-popup" in captured["inner_argv"]
        assert "ws" in captured["inner_argv"]
        assert "--popup-socket" in captured["decider_argv"]
        assert "--popup-session" in captured["decider_argv"]
        # The decider session name and the wrapper session_names pair name
        # the SAME hidden session (one suffix, one invocation, #2692).
        names = captured["session_names"]
        decider_session = captured["decider_argv"][
            captured["decider_argv"].index("--popup-session") + 1
        ]
        assert decider_session == names[1]
        assert re.match(r"klangk-shell-wsid-p\d+-[0-9a-f]+$", names[0])
        assert re.match(r"klangk-consent-wsid-p\d+-[0-9a-f]+$", names[1])

    def test_run_consent_popup_prints_disconnect_line(self, capsys):
        """After the attach returns the user sees a clean exit line, so
        tmux's own `[exited]` (and the returned-to dead screen) reads as
        a clean disconnect, not a crash (#2685 follow-up)."""
        ws = SimpleNamespace(id="wsid", name="mork")

        def fake_run(**kwargs):
            return 0

        with (
            patch(
                "klangk.cli.shellcmd.run_consent_shell", side_effect=fake_run
            ),
            patch(
                "klangk.cli.context.server_url", return_value="http://server"
            ),
        ):
            rc = cli_main.run_consent_popup(ws, "@1", True)
        assert rc == 0
        out = capsys.readouterr()
        assert "Disconnected from mork" in out.err
