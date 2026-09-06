"""Contract tests for the fmtk harness (#2881).

``scripts/fmtk-up.sh`` (the ``fmtk-up`` devenv script) composes the scratch
backend, origin-splitting proxy, fixture seed, and debug flutter run that an
agent needs to drive the live frontend with fmtk; ``scripts/fmtk-chrome.sh``
is the CHROME_EXECUTABLE URL-rewriting wrapper; ``scripts/fmtk-seed.py``
(``fmtk-seed``) seeds the fixture; ``scripts/fmtk-down.sh`` (``fmtk-down``)
stops the services fmtk-up keeps alive. These grep-style tests pin the
wiring so a future edit that silently drops a piece is loud — in the spirit
of ``test_test_push_task.py``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"
_UP = _REPO_ROOT / "scripts" / "fmtk-up.sh"
_DOWN = _REPO_ROOT / "scripts" / "fmtk-down.sh"
_CHROME = _REPO_ROOT / "scripts" / "fmtk-chrome.sh"
_SEED = _REPO_ROOT / "scripts" / "fmtk-seed.py"
_AGENTS = _REPO_ROOT / "AGENTS.md"


def test_shell_scripts_exist_and_are_executable():
    for script in (_UP, _DOWN, _CHROME):
        assert script.is_file(), f"{script} is missing"
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"{script} must be executable"
        first = script.read_text().splitlines()[0]
        assert first.startswith("#!"), f"{script} needs a shebang"


def test_seed_script_exists():
    assert _SEED.is_file(), "scripts/fmtk-seed.py is missing"


def test_devenv_wires_the_scripts():
    nix = _DEVENV_NIX.read_text()
    for name, path in (
        ("fmtk-up", "fmtk-up.sh"),
        ("fmtk-down", "fmtk-down.sh"),
        ("fmtk-seed", "fmtk-seed.py"),
    ):
        assert f"scripts.{name}.exec" in nix, (
            f"devenv.nix no longer defines scripts.{name}.exec"
        )
        assert f"scripts/{path}" in nix, f"{name} must delegate to the script"


def assert_proxy_split(up: str) -> None:
    """same-origin split: /api and /ws to the backend, rest to the dev
    server."""
    assert "handle /api/*" in up and "handle /ws" in up, (
        "the proxy must route /api/* and /ws to the backend"
    )
    assert "handle /ws/*" in up, (
        "the proxy must route the backend's other WS endpoints "
        "(/ws/consent-decider, #3237) — an unrouted decider socket makes "
        "the monitor fail-close every held egress as an instant RST"
    )


def test_up_boots_all_pieces():
    """The harness must compose backend, proxy, seed, and flutter run."""
    up = _UP.read_text()
    assert "klangk.main --config" in up, "fmtk-up must boot a scratch klangkd"
    assert "caddy run" in up, "fmtk-up must start the origin-splitting proxy"
    assert "fmtk-seed.py" in up, "fmtk-up must run the fixture seed"
    assert "flutter run --debug -d chrome" in up, (
        "fmtk-up must start the debug flutter run"
    )
    assert_proxy_split(up)


def test_up_reuses_kept_services_for_fast_relaunch():
    """Launch speed (#2881): the harness must reuse a healthy kept
    backend + proxy instead of re-booting them, and must not re-run pub
    get when deps are already resolved."""
    up = _UP.read_text()
    assert "backend_is_ours_and_healthy" in up, (
        "fmtk-up must health-check OUR scratch backend before reusing it"
    )
    assert "reusing running scratch klangkd" in up, "backend reuse path"
    assert "reusing caddy proxy" in up, "proxy reuse path"
    assert "--no-pub" in up, "skip pub get when package_config is fresh"


def assert_no_debug_port_flag(chrome: str) -> None:
    """The wrapper must not add --remote-debugging-port (flutter's own flag
    wins; the port is discovered from the process list instead)."""
    active = [ln for ln in chrome.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--remote-debugging-port" in ln for ln in active)


def test_up_uses_the_chrome_wrapper():
    """The debug run must load the app through the proxy via the wrapper."""
    assert "fmtk-chrome.sh" in _UP.read_text(), (
        "fmtk-up must set CHROME_EXECUTABLE to the URL-rewriting wrapper"
    )
    chrome = _CHROME.read_text()
    assert "8124" in chrome, "the wrapper must rewrite to the proxy origin"
    assert_no_debug_port_flag(chrome)


def test_down_stops_the_right_processes():
    """fmtk-down must target only harness-owned processes (bracket-trick
    patterns, state-dir-scoped) and support --wipe."""
    down = _DOWN.read_text()
    for pattern in (
        "run --debug -d chrome",
        "[c]hrome.*127.0.0.1:$PROXY_PORT",
        "klangk[.]main --config",
        "[c]addy run --config",
        "--wipe",
    ):
        assert pattern in down, f"fmtk-down must handle: {pattern}"
    assert 'PROXY_PORT="${FMTK_PROXY_PORT:-8124}"' in down, (
        "the chrome pattern must follow FMTK_PROXY_PORT (side-by-side "
        "harnesses on overridden ports, #3232)"
    )


def assert_removed_fixtures_absent(seed: str) -> None:
    """The pre-#2881 fixture names are gone."""
    for removed in ("fmtk-sharer", "fmtk-acler", "fmtk-viewer"):
        assert removed not in seed, f"the {removed} fixture was removed"


def assert_role_buckets_seeded(seed: str) -> None:
    """One fixture member per role bucket."""
    for role in ("collaborators", "coders", "spectators"):
        assert f'"{role}"' in seed, f"a fixture must sit in {role}"


def test_seed_matrix_wiring():
    """Fixture wiring: admin-group owner plus one member per role bucket.

    The workspace must be created with fmtk-admin's token so it owns it
    (``GET /workspaces`` lists only owned workspaces). Every role group
    carries ``terminal``, so each member opens the workspace page (the WS
    ``workspace_connect`` gate requires it — #2881 pain point 2).
    """
    seed = _SEED.read_text()
    assert "fmtk-admin" in seed and '"admins"' in seed, (
        "fmtk-admin must exist and join the admins group"
    )
    assert_removed_fixtures_absent(seed)
    assert_role_buckets_seeded(seed)
    assert "owner_token" in seed, (
        "the workspace must be created with fmtk-admin's token (ownership)"
    )


def test_seed_clears_must_change_password():
    """Admin-created users carry must_change_password (#3172), which
    refuses every API call but the change flow — the seed must clear it
    for every fixture user on every run or fixture logins dead-end —
    EXCEPT fmtk-mustchange, the forced-change fixture (#3233), whose
    flag is the point."""
    seed = _SEED.read_text()
    assert '"must_change_password": False' in seed, (
        "the seed must PATCH must_change_password off for fixture users"
    )
    assert 'KEEP_MUST_CHANGE = {"fmtk-mustchange"}' in seed, (
        "the forced-change fixture must keep its admin-set flag (#3233)"
    )
    for fixture in ("fmtk-mustchange", "fmtk-reset"):
        assert f'"{fixture}"' in seed, f"{fixture} must be seeded (#3233)"


def assert_wired(source: str, needles: tuple[str, ...], why: str) -> None:
    """Every ``needle`` must appear in ``source`` (drop-in wiring pins)."""
    for needle in needles:
        assert needle in source, f"{why}: {needle!r} missing"


def test_harness_auth_suite_extensions():
    """The auth-suite harness pieces (#3233) — SMTP sink for email-token
    flows, hash-route navigation, auth-service evaluation, admin API —
    must stay wired into the harness."""
    harness = (_REPO_ROOT / "src/frontend/e2e-tests/fmtk/fmtkharness.py").read_text()
    assert_wired(
        harness,
        (
            "class SmtpSink",
            # RFC 5321 dot-unstuffing — a stuffed dot at a JWT separator
            # corrupts the extracted link token (#3238)
            'if line.startswith(b".."):\n                line = line[1:]',
            '"smtp_host"',
            "def navigate",
            "def auth_eval",
            "def logout",
            "def force_password_change",
        ),
        "the auth-suite harness pieces (#3233) must stay wired in",
    )
    suite = _REPO_ROOT / "src/frontend/e2e-tests/fmtk/test_auth.py"
    assert suite.is_file(), "the auth suite (#3233) is missing"
    assert "token_for(" in suite.read_text(), (
        "the auth suite must drive email-token flows via the sink"
    )


def test_workspace_suite_extensions():
    """The workspace-suite harness pieces (#3234) — exact-label tile
    taps, label/identifier waits, scroll-to-reveal, binary downloads —
    must stay wired into the harness, and the suite must drive the
    instrumented identifiers."""
    harness = (_REPO_ROOT / "src/frontend/e2e-tests/fmtk/fmtkharness.py").read_text()
    assert_wired(
        harness,
        (
            "def tap_labeled_exact",
            "def tap_button_exact",
            "def wait_for_label",
            "def wait_for_identifier",
            "def wait_identifier_gone",
            "def scroll_until_label",
            "def http_download",
            "def find_label_nodes",
            "def parent_map",
        ),
        "the workspace-suite locators (#3234) must stay wired in",
    )
    suite = _REPO_ROOT / "src/frontend/e2e-tests/fmtk/test_workspaces.py"
    assert suite.is_file(), "the workspace suite (#3234) is missing"
    assert_wired(
        suite.read_text(),
        (
            'tap_label("Create workspace")',
            'tap_label("Import")',
            "tap_button_exact",
            "testPickFileBytesOverride",
            "per_handle_home",
            "container-stopped-overlay",
            "file-browser-path",
        ),
        "the workspace suite (#3234) must drive the instrumented UI",
    )


def test_terminal_suite_extensions():
    """The terminal suite (#3235) drives the canvas terminal through
    the GhosttyTerminalState eval (buffer assertions, never the widget
    tree) and the instrumented tab-strip labels."""
    suite = _REPO_ROOT / "src/frontend/e2e-tests/fmtk/test_terminals.py"
    assert suite.is_file(), "the terminal suite (#3235) is missing"
    assert_wired(
        suite.read_text(),
        (
            "terminal_buffer",
            "terminal_send",
            "terminal_eval",
            "jsonEncode(st!.widget.wsClient.terminalWindows)",
            "jsonEncode(st!.widget.wsClient.sharedTerminals)",
            'tap_labeled_exact("New terminal")',
            "Close ",
            "Shared:",
            "debug_dump_focus_tree",
            "allow_sudo",
        ),
        "the terminal suite (#3235) must drive the buffer and the instrumented strip",
    )
    strip = (
        _REPO_ROOT / "src/frontend/lib/workspace/terminal_tabs_view.dart"
    ).read_text()
    assert_wired(
        strip,
        (
            "semanticLabel: widget.tooltip",
            "semanticLabel: 'Close ${widget.name}'",
            "semanticLabel: 'Unshare'",
            "semanticLabel:",
        ),
        "the terminal strip instrumentation (#3235) must stay wired in",
    )


def test_network_suite_extensions():
    """The network suite (#3237) drives the consent surface end to end:
    egress attempts from the real terminal, verdicts from the banner,
    and rule/pause management from the panel."""
    suite = _REPO_ROOT / "src/frontend/e2e-tests/fmtk/test_network.py"
    assert suite.is_file(), "the network suite (#3237) is missing"
    assert_wired(
        suite.read_text(),
        (
            "Pending egress consent",
            "Egress consent rules",
            "Active denies (1)",
            "Active allows (1)",
            "Static allow-list",
            "Pause 15m",
            "Unpause",
            "Filtering paused",
            "Revoke consent rule?",
            '"egress_mode": "static"',
            '"egress_mode": "interactive"',
            'f"EG-{EG_SEQ}"',  # per-attempt exit-code tags
            'f"{host}:"',  # the email-chip-immune host match
        ),
        "the network suite (#3237) must drive the banner, the panel, "
        "and the terminal-outcome loop",
    )


def test_sharing_suite_extensions():
    """The sharing suite (#3238) drives the Sharing tab's role buckets,
    the advanced ACL editor, and the permission gates through the
    instrumented labels."""
    suite = _REPO_ROOT / "src/frontend/e2e-tests/fmtk/test_sharing.py"
    assert suite.is_file(), "the sharing suite (#3238) is missing"
    assert_wired(
        suite.read_text(),
        (
            "tap_overlay_labeled_exact",
            'tap_labeled_exact("Add to spectators")',
            'f"Remove {FRESH_EMAIL}"',
            'f"Remove entry {email} {permission}"',
            '"share-workspace"',
            '"Access to this workspace has been revoked"',
            '"Permission denied"',
            '"Re-authentication required"',
            "step_up_window_minutes",
            '"admins"',
        ),
        "the sharing suite (#3238) must drive the buckets, the editor, and the gates",
    )
    panel = (
        _REPO_ROOT / "src/frontend/lib/workspace/workspace_sharing_panel.dart"
    ).read_text()
    assert_wired(
        panel,
        (
            "semanticLabel: 'Add to ${roleName}'",
            "semanticLabel: 'Remove ${m['email']}'",
        ),
        "the sharing panel instrumentation (#3238) must stay wired in",
    )
    editor = (_REPO_ROOT / "src/frontend/lib/widgets/acl_editor.dart").read_text()
    assert_wired(
        editor,
        (
            "semanticLabel: 'Remove entry $principal $permission'",
            "hint: const Text('Select user')",
        ),
        "the ACL editor instrumentation (#3238) must stay wired in",
    )


def test_agents_documents_the_harness():
    agents = _AGENTS.read_text()
    assert "fmtk-up" in agents, "AGENTS.md must point at the fmtk-up harness"
    assert "fmtk-down" in agents, "AGENTS.md must document fmtk-down"
    assert "test-fmtk-e2e" in agents, (
        "AGENTS.md must document the automated fmtk e2e runner (#3232)"
    )


def assert_suite_files_present(suite_dir: Path) -> None:
    """The pytest suite pieces must exist together (#3232)."""
    for name in ("fmtkharness.py", "conftest.py", "test_smoke.py"):
        assert (suite_dir / name).is_file(), f"{name} is missing from {suite_dir}"


def test_e2e_suite_wiring():
    """The fmtk-driven e2e suite (#3232): devenv script, library, tests,
    and CI workflow must all exist together."""
    nix = _DEVENV_NIX.read_text()
    assert "scripts.test-fmtk-e2e.exec" in nix, (
        "devenv.nix must define the test-fmtk-e2e script"
    )
    assert_suite_files_present(_REPO_ROOT / "src/frontend/e2e-tests/fmtk")
    workflow = _REPO_ROOT / ".github/workflows/fmtk-e2e-tests.yml"
    assert workflow.is_file(), "the fmtk e2e CI workflow is missing"
    assert "test-fmtk-e2e" in workflow.read_text()
    # the suites start real containers (workspace + network sidecar, the
    # interactive-egress gate fail-closes without the sidecar image) — CI
    # must build both images, not run image-less smoke suites only
    wf = workflow.read_text()
    assert 'build-tasks: ""' not in wf, (
        "the fmtk e2e workflow must build the workspace + sidecar images"
    )
