"""Backend end-to-end tests against a real Klangk server.

These tests start a real uvicorn server, run klangk CLI commands as
subprocesses, and verify behavior against real podman containers.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e
"""

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from klangk.model import free_port
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_env import clean_env
from _e2e_server import start_server, stop_server, tracked_mkdtemp

logger = logging.getLogger(__name__)


def run(args, timeout=120, input=None, **kwargs):
    """Run a CLI command, return CompletedProcess."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
        **kwargs,
    )


def _start_server(data_dir, port=None, extra_env=None):
    """Start a real klangkd (the proxy on a TCP port) and wait for readiness.

    Returns (server_handle, base_url). The CLI drives the real ``klangk``
    binary against ``base_url`` — the proxy proxies to klangkd's UDS, as in
    production (#1525). Server output is streamed to a file (not a pipe)
    so a long-lived run can't fill the 64 KB OS pipe buffer and deadlock
    (#364).
    """
    log_path = os.path.join(data_dir, "server.log")
    overrides = {
        "KLANGKD_JWT_SECRET": "cli-e2e-test-secret",
        "KLANGKD_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGKD_DEFAULT_USER": "test@example.com",
        "KLANGKD_DEFAULT_PASSWORD": "testpass",
        "KLANGKD_TEST_MODE": "1",
        "KLANGKD_IDLE_TIMEOUT_SECONDS": "300",
        "LOGFIRE_TOKEN": "",
    }
    if port is not None:
        overrides["KLANGKD_PORT"] = port
    if extra_env:
        overrides.update(extra_env)
    server = start_server(
        uds=False, data_dir=data_dir, log_path=log_path, **overrides
    )
    return server, server["url"]


def _stop_server(server, data_dir=None):
    """Stop a server started by ``_start_server``."""
    stop_server(server)


@pytest.fixture(scope="session")
def server():
    """Start a real Klangk server for the test session."""
    data_dir = tracked_mkdtemp("klangk-cli-e2e-")
    port = str(free_port())
    proc, base_url = _start_server(data_dir, port)
    yield {
        "url": base_url,
        "port": port,
        "data_dir": data_dir,
        "proc": proc,
    }
    _stop_server(proc, data_dir)


@pytest.fixture(scope="session")
def cli_config(server, tmp_path_factory):
    """Create a CLI config pointing at the test server."""
    config_dir = tmp_path_factory.mktemp("klangk-cli-config")
    env = clean_env(HOME=str(config_dir))
    # The CLI reads from ~/.config/klangk/klangk.yaml
    klangk_config_dir = config_dir / ".config" / "klangk"
    klangk_config_dir.mkdir(parents=True)
    return {
        "env": env,
        "config_dir": klangk_config_dir,
        "config_file": klangk_config_dir / "klangk.yaml",
        "server_url": server["url"],
    }


@pytest.fixture(autouse=True, scope="session")
def _ensure_login(cli_config):
    """Log in once for the entire test session.

    This allows any test class to run in isolation with -k without
    depending on TestLogin having run first.
    """
    run(
        [
            "klangk",
            "login",
            cli_config["server_url"],
            "test@example.com",
            "--password-file",
            "-",
        ],
        input="testpass\n",
        env=cli_config["env"],
    )


class TestLogin:
    def test_login_with_email_arg(self, server, cli_config):
        result = run(
            [
                "klangk",
                "login",
                server["url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert (
            "Logged in" in result.stdout
            or "Already logged in" in result.stdout
        )
        # Config file should exist now
        assert cli_config["config_file"].exists()

    def test_login_reuses_token(self, server, cli_config):
        result = run(
            [
                "klangk",
                "login",
                server["url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Already logged in" in result.stdout

    def test_status_shows_logged_in(self, cli_config):
        result = run(
            ["klangk", "status", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "status=logged_in" in result.stdout
        assert "test@example.com" in result.stdout


class TestWorkspaceCRUD:
    def test_create_workspace(self, cli_config):
        result = run(
            ["klangk", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_list_workspaces(self, cli_config):
        result = run(
            ["klangk", "ls", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_create_duplicate_fails(self, cli_config):
        result = run(
            ["klangk", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_nonexistent_fails(self, cli_config):
        result = run(
            ["klangk", "rm", "nonexistent-ws"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_workspace(self, cli_config):
        result = run(
            ["klangk", "rm", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Deleted" in result.stdout

    def test_list_after_delete(self, cli_config):
        result = run(
            ["klangk", "ls", "--plain"],
            env=cli_config["env"],
        )
        assert "e2e-crud" not in result.stdout


class TestDuplicate:
    @staticmethod
    def _login(cli_config):
        env = cli_config["env"]
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_dup_workspace(self, cli_config):
        env = cli_config["env"]
        TestDuplicate._login(cli_config)
        run(
            ["klangk", "create", "e2e-dup-src", "--env", "FOO=bar"],
            env=env,
        )
        try:
            result = run(
                ["klangk", "dup", "e2e-dup-src", "e2e-dup-copy"],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-dup-copy" in result.stdout

            # Verify copy appears in list
            result = run(
                ["klangk", "ls", "--plain"],
                env=env,
            )
            assert "e2e-dup-src" in result.stdout
            assert "e2e-dup-copy" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-dup-copy"], env=env)
            run(["klangk", "rm", "e2e-dup-src"], env=env)

    def test_dup_nonexistent(self, cli_config):
        env = cli_config["env"]
        TestDuplicate._login(cli_config)
        result = run(
            ["klangk", "dup", "no-such-ws", "copy"],
            env=env,
        )
        assert result.returncode != 0


class TestExec:
    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def workspace(cli_config):
        run(["klangk", "create", "e2e-exec"], env=cli_config["env"])
        yield
        run(["klangk", "rm", "e2e-exec"], env=cli_config["env"])

    def test_exec_echo(self, cli_config):
        result = run(
            ["klangk", "exec", "e2e-exec", "echo", "hello from exec"],
            env=cli_config["env"],
            timeout=120,
        )
        assert result.returncode == 0
        assert "hello from exec" in result.stdout

    def test_exec_piped_stdin(self, cli_config):
        result = run(
            ["klangk", "exec", "e2e-exec", "cat"],
            input="piped data\n",
            env=cli_config["env"],
            timeout=120,
        )
        assert result.returncode == 0
        assert "piped data" in result.stdout

    def test_exec_exit_code(self, cli_config):
        result = run(
            ["klangk", "exec", "e2e-exec", "false"],
            env=cli_config["env"],
            timeout=120,
        )
        assert result.returncode != 0

    def test_exec_yes_backpressure(self, cli_config):
        """Smoke test: run `yes` briefly to exercise bounded queue back-pressure."""
        result = run(
            [
                "klangk",
                "exec",
                "e2e-exec",
                "bash",
                "-c",
                "yes | head -1000",
            ],
            env=cli_config["env"],
            timeout=30,
        )
        assert result.returncode == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1000
        assert all(line == "y" for line in lines)


class TestSync:
    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def workspace(cli_config):
        run(["klangk", "create", "e2e-sync"], env=cli_config["env"])
        yield
        run(["klangk", "rm", "e2e-sync"], env=cli_config["env"])

    def test_sync_to_container(self, cli_config, tmp_path):
        # Create local files
        src = tmp_path / "sync-src"
        src.mkdir()
        (src / "file1.txt").write_text("content one")
        (src / "file2.txt").write_text("content two")

        result = run(
            [
                "klangk",
                "sync",
                str(src) + "/",
                "e2e-sync:/home/klangk/synced/",
            ],
            env=cli_config["env"],
            timeout=120,
        )
        assert result.returncode == 0

        # Verify files arrived
        verify = run(
            [
                "klangk",
                "exec",
                "e2e-sync",
                "cat",
                "/home/klangk/synced/file1.txt",
            ],
            env=cli_config["env"],
            timeout=120,
        )
        assert verify.returncode == 0
        assert "content one" in verify.stdout

    def test_sync_from_container(self, cli_config, tmp_path):
        # Create a file in the container
        run(
            [
                "klangk",
                "exec",
                "e2e-sync",
                "bash",
                "-c",
                "echo remote-data > /home/klangk/remote-file.txt",
            ],
            env=cli_config["env"],
            timeout=120,
        )

        dest = tmp_path / "sync-dest"
        dest.mkdir()

        result = run(
            [
                "klangk",
                "sync",
                "e2e-sync:/home/klangk/remote-file.txt",
                str(dest) + "/",
            ],
            env=cli_config["env"],
            timeout=120,
        )
        assert result.returncode == 0
        assert (dest / "remote-file.txt").read_text().strip() == "remote-data"


class TestSyncLarge:
    """Test syncing directories with 10+ MB of data."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def workspace(cli_config):
        run(["klangk", "create", "e2e-sync-large"], env=cli_config["env"])
        yield
        run(["klangk", "rm", "e2e-sync-large"], env=cli_config["env"])

    def _make_large_tree(self, root, rng, target_bytes=10 * 1024 * 1024):
        """Create a directory tree with ~target_bytes of data."""
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        file_num = 0
        # Create files across several subdirectories
        for subdir_idx in range(5):
            subdir = root / f"dir{subdir_idx}" / "nested"
            subdir.mkdir(parents=True, exist_ok=True)
            while total < target_bytes * (subdir_idx + 1) // 5:
                size = rng.randint(50_000, 500_000)
                data = bytes(rng.getrandbits(8) for _ in range(size))
                (subdir / f"file{file_num}.bin").write_bytes(data)
                total += size
                file_num += 1
        return total, file_num

    def test_sync_large_to_container(self, cli_config, tmp_path):
        import hashlib
        import random

        env = cli_config["env"]
        rng = random.Random(42)
        src = tmp_path / "large-src"
        total_bytes, file_count = self._make_large_tree(src, rng)
        assert total_bytes >= 10 * 1024 * 1024

        # Hash every file for later comparison
        src_hashes = {}
        for f in sorted(src.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(src))
                src_hashes[rel] = hashlib.sha256(f.read_bytes()).hexdigest()

        # Sync to container
        result = run(
            [
                "klangk",
                "sync",
                str(src) + "/",
                "e2e-sync-large:/home/klangk/large-upload/",
            ],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0

        # Verify file count in container
        verify = run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "find /home/klangk/large-upload -type f | wc -l",
            ],
            env=env,
            timeout=120,
        )
        assert verify.returncode == 0
        assert int(verify.stdout.strip()) == file_count

        # Verify total size in container
        verify = run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "du -sb /home/klangk/large-upload | cut -f1",
            ],
            env=env,
            timeout=120,
        )
        assert verify.returncode == 0
        container_size = int(verify.stdout.strip())
        assert container_size >= 10 * 1024 * 1024

        # Spot-check a few file hashes via exec
        for rel, expected_hash in list(src_hashes.items())[:3]:
            verify = run(
                [
                    "klangk",
                    "exec",
                    "e2e-sync-large",
                    "sha256sum",
                    f"/home/klangk/large-upload/{rel}",
                ],
                env=env,
                timeout=120,
            )
            assert verify.returncode == 0
            assert expected_hash in verify.stdout

    def test_sync_large_from_container(self, cli_config, tmp_path):
        import hashlib

        env = cli_config["env"]

        # Create large data in the container
        run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "mkdir -p /home/klangk/large-download && "
                "for i in $(seq 1 25); do "
                "dd if=/dev/urandom of=/home/klangk/large-download/file$i.bin "
                "bs=1024 count=420 status=none; done",
            ],
            env=env,
            timeout=120,
        )

        # Verify size in container (~10.5 MB)
        verify = run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "du -sb /home/klangk/large-download | cut -f1",
            ],
            env=env,
            timeout=120,
        )
        assert verify.returncode == 0
        assert int(verify.stdout.strip()) >= 10 * 1024 * 1024

        # Get hashes in container
        verify = run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "sha256sum /home/klangk/large-download/*.bin",
            ],
            env=env,
            timeout=120,
        )
        assert verify.returncode == 0
        container_hashes = {}
        for line in verify.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                h, path = parts
                fname = path.rsplit("/", 1)[-1]
                container_hashes[fname] = h

        # Sync from container
        dest = tmp_path / "large-dest"
        dest.mkdir()

        result = run(
            [
                "klangk",
                "sync",
                "e2e-sync-large:/home/klangk/large-download/",
                str(dest) + "/",
            ],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0

        # Verify all files arrived with correct hashes
        local_files = sorted(dest.rglob("*.bin"))
        assert len(local_files) == 25
        for f in local_files:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            assert h == container_hashes[f.name], f"Hash mismatch: {f.name}"


class TestServiceCommand:
    @staticmethod
    def _login(cli_config):
        env = cli_config["env"]
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_service_command_stored_in_workspace(self, cli_config):
        """service_command is stored in the workspace via the API."""
        env = cli_config["env"]
        TestServiceCommand._login(cli_config)
        run(["klangk", "create", "e2e-defcmd"], env=env)
        try:
            # Set command
            result = run(
                ["klangk", "edit", "e2e-defcmd", "--command", "echo hello"],
                env=env,
            )
            assert result.returncode == 0, result.stdout
            assert "Updated" in result.stdout

            # Clear
            result = run(
                ["klangk", "edit", "e2e-defcmd", "--command", ""], env=env
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-defcmd"], env=env)


class TestAutoStart:
    """Verify auto-start workspace creation and editing."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def autostart_server(tmp_path_factory, request):
        data_dir = tracked_mkdtemp("klangk-autostart-")
        proc, base_url = _start_server(
            data_dir,
            str(free_port()),
            extra_env={"KLANGKD_ALLOW_AUTOSTART": "1"},
        )
        config_dir = tmp_path_factory.mktemp("klangk-autostart-config")
        env = clean_env(HOME=str(config_dir))
        klangk_config_dir = config_dir / ".config" / "klangk"
        klangk_config_dir.mkdir(parents=True)
        run(
            [
                "klangk",
                "login",
                base_url,
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        request.cls._env = env
        request.cls._base_url = base_url
        yield
        _stop_server(proc, data_dir)

    def test_create_with_auto_start(self):
        env = self._env
        try:
            result = run(
                ["klangk", "create", "e2e-autostart", "--auto-start"],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-autostart" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-autostart"], env=env)

    def test_edit_auto_start_on_off(self):
        env = self._env
        run(["klangk", "create", "e2e-autostart2"], env=env)
        try:
            result = run(
                ["klangk", "edit", "e2e-autostart2", "--auto-start"],
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout

            result = run(
                ["klangk", "edit", "e2e-autostart2", "--no-auto-start"],
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-autostart2"], env=env)

    def test_create_auto_start_rejected_without_env(self, cli_config):
        """auto_start=True is rejected when KLANGKD_ALLOW_AUTOSTART is unset."""
        env = cli_config["env"]
        result = run(
            ["klangk", "create", "e2e-autostart-no", "--auto-start"],
            env=env,
        )
        assert result.returncode != 0
        assert "not enabled" in result.stdout or "not enabled" in result.stderr


def _poll_exec(env, workspace, shell_cmd, expect, timeout=30, interval=1.0):
    """Run ``klangk exec`` repeatedly until *expect* appears in stdout.

    The service command runs asynchronously in a tmux window, so its
    effect isn't visible the instant ``klangk sandbox`` returns.  This
    polls until it is (or fails loudly on timeout).
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        r = run(
            ["klangk", "exec", workspace, "bash", "-c", shell_cmd],
            env=env,
            timeout=30,
        )
        last = r.stdout
        if expect in r.stdout:
            return r.stdout
        time.sleep(interval)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {expect!r} from"
        f" `klangk exec {workspace} {shell_cmd}`; last stdout:"
        f" {last!r}"
    )


class TestSandboxAutoStartServiceCommand:
    """Auto-started workspace: service_command runs only after setup.

    This is the path PR #1032 guarantees: when a workspace is created
    with ``auto_start`` the service command is *not* run eagerly at
    container start (the software isn't installed yet).  ``klangk
    sandbox`` runs ``setup.sh`` (which installs the command), then sends
    ``terminal_start``, which launches the command in a dedicated tmux
    window.  We verify the command actually ran *and* that it only
    succeeded after the thing ``setup.sh`` installs became available --
    which fails if the eager-start path regresses to running the default
    command before setup.
    """

    WS = "e2e-sandbox-defcmd"
    # Free port (allocated once at class-definition time) so this never
    # collides with the other class-scoped servers or the session server.
    PORT = str(free_port())

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def autostart_server(tmp_path_factory, request):
        data_dir = tracked_mkdtemp("klangk-sandbox-defcmd-")
        proc, base_url = _start_server(
            data_dir,
            TestSandboxAutoStartServiceCommand.PORT,
            extra_env={"KLANGKD_ALLOW_AUTOSTART": "1"},
        )
        config_dir = tmp_path_factory.mktemp("klangk-sandbox-defcmd-config")
        env = clean_env(HOME=str(config_dir))
        klangk_config_dir = config_dir / ".config" / "klangk"
        klangk_config_dir.mkdir(parents=True)
        run(
            [
                "klangk",
                "login",
                base_url,
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        request.cls._env = env
        yield
        _stop_server(proc, data_dir)

    def test_service_command_runs_only_after_setup(self, tmp_path):
        """service_command (installed by setup.sh) runs post-setup.

        ``setup.sh`` sleeps ~5s to simulate installing software, then
        creates ``/tmp/myapp``.  The workspace's service_command is
        ``/tmp/myapp``, which does not exist until setup runs -- so it
        can only succeed once the sandbox setup phase has completed.
        """
        env = self._env

        sandbox_root = tmp_path / "sb"
        sandbox_root.mkdir()
        (sandbox_root / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n"
            "  mount-at: /sandbox\n"
            "  setup: setup.sh\n"
            "workspace:\n"
            "  service-command: /tmp/myapp\n"
            "  auto-start: true\n"
        )
        # setup.sh: slow "install", then drop a marker-writing script at
        # /tmp/myapp and record (epoch seconds) when setup finished.
        # Must be executable: sandbox_setup runs it directly
        # (``bash -c '/sandbox/setup.sh'``), not via ``bash <script>``.
        setup_sh = sandbox_root / "setup.sh"
        setup_sh.write_text(
            "#!/bin/sh\n"
            "# Simulate a slow software install (e.g. openclaw).\n"
            "sleep 5\n"
            "cat > /tmp/myapp <<'APP'\n"
            "#!/bin/sh\n"
            "date +%s > /tmp/service-cmd-when\n"
            "echo ran > /tmp/service-cmd-ran\n"
            "APP\n"
            "chmod +x /tmp/myapp\n"
            "date +%s > /tmp/setup-done\n"
        )
        setup_sh.chmod(0o755)
        try:
            # Sandbox creates the auto-start workspace, runs setup.sh
            # (sleep 5 + install), then sends terminal_start so the
            # service command runs in the persistent service-cmd window.
            result = run(
                ["klangk", "sandbox", self.WS, str(sandbox_root)],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr

            # (1) The service command actually ran (async in tmux).
            _poll_exec(
                env,
                self.WS,
                "cat /tmp/service-cmd-ran 2>/dev/null",
                expect="ran",
                timeout=30,
            )

            # (2) setup.sh did install /tmp/myapp -- proving setup ran
            # and therefore myapp did NOT exist at eager-start time.
            installed = run(
                ["klangk", "exec", self.WS, "test", "-x", "/tmp/myapp"],
                env=env,
                timeout=30,
            )
            assert installed.returncode == 0, (
                "/tmp/myapp missing or not executable; setup never installed it"
            )

            # (3) The service command ran at/after setup completed.
            # setup-done is written last in setup.sh; service-cmd-when is
            # written by myapp when the service command runs.  The service
            # command only fires once setup is complete (it is gated on
            # setup_state), so service-cmd-when is written after setup-done.
            ordering = run(
                [
                    "klangk",
                    "exec",
                    self.WS,
                    "bash",
                    "-c",
                    '[ "$(cat /tmp/setup-done)" -le'
                    ' "$(cat /tmp/service-cmd-when)" ] && echo ordered',
                ],
                env=env,
                timeout=30,
            )
            assert "ordered" in ordering.stdout, (
                "service command ran before setup completed: "
                f"{ordering.stdout!r}"
            )
        finally:
            run(["klangk", "rm", self.WS], env=env, timeout=60)


class TestMounts:
    @staticmethod
    def _login(cli_config):
        env = cli_config["env"]
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_create_with_mount_flag(self, cli_config):
        env = cli_config["env"]
        TestMounts._login(cli_config)
        try:
            result = run(
                [
                    "klangk",
                    "create",
                    "e2e-mount",
                    "--mount",
                    "/tmp:/mnt/tmp",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-mount" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-mount"], env=env)

    def test_edit_with_mount_flags(self, cli_config):
        env = cli_config["env"]
        TestMounts._login(cli_config)
        run(["klangk", "create", "e2e-mount-edit"], env=env)
        try:
            result = run(
                [
                    "klangk",
                    "edit",
                    "e2e-mount-edit",
                    "--mount",
                    "/tmp:/mnt/a",
                    "--mount",
                    "/tmp:/mnt/b",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-mount-edit"], env=env)

    def test_edit_interactive_add_mount(self, cli_config):
        env = cli_config["env"]
        TestMounts._login(cli_config)
        run(["klangk", "create", "e2e-mount-int"], env=env)
        try:
            # Interactive: keep name, keep image, keep command, keep
            # health check, keep classification banner (5 Enter prompts
            # before the mount loop), add mount "/tmp:/mnt/test", skip the
            # loop's re-prompted add, skip remove, skip add env, skip
            # allowed-domains add, skip rejected-domains add (11 lines; the
            # add-prompt re-appears after each successful add). The input
            # lines map 1:1 onto the prompts — a mis-counted newline
            # silently lands the mount spec on an earlier _prompt and the
            # edit still reports "Updated", so the mount is verified
            # explicitly below.
            result = run(
                ["klangk", "edit", "e2e-mount-int"],
                input="\n\n\n\n\n/tmp:/mnt/test\n\n\n\n\n\n",
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
            # Verify the mount actually landed (and did not clobber the
            # banner or health check): re-run the interactive edit with
            # keep-everything input and inspect the echoed current state.
            verify = run(
                ["klangk", "edit", "e2e-mount-int"],
                input="\n" * 10,
                env=env,
            )
            assert verify.returncode == 0
            assert "/tmp:/mnt/test" in verify.stdout
            assert "Classification banner [(none)]" in verify.stdout
            assert "Health check command [(none)]" in verify.stdout
        finally:
            run(["klangk", "rm", "e2e-mount-int"], env=env)


class TestEnvVars:
    @staticmethod
    def _login(cli_config):
        env = cli_config["env"]
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_create_with_env_flag(self, cli_config):
        env = cli_config["env"]
        TestEnvVars._login(cli_config)
        try:
            result = run(
                [
                    "klangk",
                    "create",
                    "e2e-env",
                    "--env",
                    "FOO=bar",
                    "--env",
                    "BAZ=qux",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-env" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-env"], env=env)

    def test_edit_with_env_flag(self, cli_config):
        env = cli_config["env"]
        TestEnvVars._login(cli_config)
        run(["klangk", "create", "e2e-env-edit"], env=env)
        try:
            result = run(
                [
                    "klangk",
                    "edit",
                    "e2e-env-edit",
                    "--env",
                    "X=1",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-env-edit"], env=env)


class TestVolumes:
    @staticmethod
    def _login(cli_config):
        env = cli_config["env"]
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_volumes_lifecycle(self, cli_config):
        env = cli_config["env"]
        TestVolumes._login(cli_config)

        # Create
        result = run(["klangk", "volumes", "create", "e2e-vol"], env=env)
        assert result.returncode == 0
        assert "Created" in result.stdout

        # List
        result = run(["klangk", "volumes", "ls", "--plain"], env=env)
        assert result.returncode == 0
        assert "e2e-vol" in result.stdout

        # Create duplicate fails
        result = run(["klangk", "volumes", "create", "e2e-vol"], env=env)
        assert result.returncode != 0

        # Remove
        result = run(["klangk", "volumes", "rm", "e2e-vol"], env=env)
        assert result.returncode == 0
        assert "Deleted" in result.stdout

        # List after delete
        result = run(["klangk", "volumes", "ls", "--plain"], env=env)
        assert "e2e-vol" not in result.stdout

    def test_volumes_rm_nonexistent(self, cli_config):
        env = cli_config["env"]
        TestVolumes._login(cli_config)
        result = run(["klangk", "volumes", "rm", "no-such-vol"], env=env)
        assert result.returncode != 0

    def test_volumes_empty_list(self, cli_config):
        env = cli_config["env"]
        TestVolumes._login(cli_config)
        result = run(["klangk", "volumes", "ls"], env=env)
        assert result.returncode == 0
        # May show "No volumes." or an empty table


class TestSudoLifecycle:
    """#3046/#3047: the per-workspace sudo posture is the stored bag
    value (absent = off; the deploy flag is only a ceiling), and it
    flips with a plain edit — each flip offers a restart that rewrites
    the sudoers rule (klangk-configure-sudo runs on every container
    start, resolving the settings bag then), so no manual container
    recreation is needed."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def workspace(cli_config):
        # No --sudo flag: the workspace starts locked down (absent key).
        run(["klangk", "create", "e2e-sudo"], env=cli_config["env"])
        yield
        run(["klangk", "rm", "e2e-sudo"], env=cli_config["env"])

    def test_sudo_off_by_default(self, cli_config):
        result = run(
            ["klangk", "exec", "e2e-sudo", "sudo", "-n", "true"],
            env=cli_config["env"],
            timeout=180,
        )
        assert result.returncode != 0  # !ALL rule denies sudo

    def test_edit_on_opts_in(self, cli_config):
        result = run(
            ["klangk", "edit", "e2e-sudo", "--sudo"],
            input="y\n",  # accept the offered restart
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Updated" in result.stdout
        result = run(
            ["klangk", "exec", "e2e-sudo", "sudo", "-n", "true"],
            env=cli_config["env"],
            timeout=180,
        )
        assert result.returncode == 0  # NOPASSWD:ALL

    def test_edit_off_locks_sudo_down(self, cli_config):
        result = run(
            ["klangk", "edit", "e2e-sudo", "--no-sudo"],
            input="y\n",  # accept the offered restart
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Updated" in result.stdout
        result = run(
            ["klangk", "exec", "e2e-sudo", "sudo", "-n", "true"],
            env=cli_config["env"],
            timeout=180,
        )
        assert result.returncode != 0  # !ALL rule denies sudo

    def test_edit_back_on_restores_sudo(self, cli_config):
        result = run(
            ["klangk", "edit", "e2e-sudo", "--sudo"],
            input="y\n",  # accept the offered restart
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Updated" in result.stdout
        result = run(
            ["klangk", "exec", "e2e-sudo", "sudo", "-n", "true"],
            env=cli_config["env"],
            timeout=180,
        )
        assert result.returncode == 0  # NOPASSWD:ALL restored


class TestAuthError:
    def test_command_without_login_shows_clean_error(self, server, tmp_path):
        """Commands that need auth should show a clean error, not a traceback."""
        # Fresh config dir with no login
        config_dir = tmp_path / "no-login"
        config_dir.mkdir()
        klangk_config = config_dir / ".config" / "klangk"
        klangk_config.mkdir(parents=True)
        env = clean_env(HOME=str(config_dir))
        result = run(
            ["klangk", "ls"],
            env=env,
        )
        assert result.returncode != 0
        assert "Traceback" not in result.stderr
        assert "login" in result.stderr.lower()


class TestLogout:
    def test_logout(self, cli_config):
        result = run(
            ["klangk", "logout"],
            env=cli_config["env"],
        )
        assert result.returncode == 0

    def test_status_after_logout(self, cli_config):
        result = run(
            ["klangk", "status", "--plain"],
            env=cli_config["env"],
        )
        assert "not_logged_in" in result.stdout


class TestExportSymlinks:
    @pytest.fixture(autouse=True)
    @staticmethod
    def _login(cli_config):
        """Ensure logged in for this test class."""
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )

    def _get_home_dir(self, server, cli_config):
        """Get the host-side home directory for a workspace.

        Finds the workspace home dir by scanning the data directory
        for workspace ID subdirectories.
        """
        from pathlib import Path

        import httpx

        resp = httpx.post(
            f"{server['url']}/api/v1/auth/login",
            json={"identifier": "test@example.com", "password": "testpass"},
        )
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = httpx.get(f"{server['url']}/api/v1/workspaces", headers=headers)
        ws = [w for w in resp.json() if w["name"] == "e2e-symlink"][0]
        ws_id = ws["id"]

        ws_root = Path(server["data_dir"]) / "workspaces"
        return ws_root / ws_id / "home"

    def test_export_preserves_all_symlinks(self, server, cli_config, tmp_path):
        """All symlinks are preserved (stored as links, not content)."""
        env = cli_config["env"]

        result = run(["klangk", "create", "e2e-symlink"], env=env)
        assert result.returncode == 0

        try:
            # Place files and symlinks directly on the host filesystem
            # (the workspace home dir is what gets archived by export).
            home_dir = self._get_home_dir(server, cli_config)
            home_dir.mkdir(parents=True, exist_ok=True)

            (home_dir / "real.txt").write_text("real content")
            # Relative symlink
            (home_dir / "relative_link").symlink_to("real.txt")
            # External absolute symlink (preserved as symlink, not content)
            (home_dir / "external_link").symlink_to("/etc/hostname")

            archive = tmp_path / "symlink-test.tar.gz"
            result = run(
                ["klangk", "export", "e2e-symlink", "-o", str(archive)],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr or result.stdout

            import tarfile

            with tarfile.open(archive, "r:gz") as tar:
                names = tar.getnames()
                assert any("real.txt" in n for n in names)
                assert any("relative_link" in n for n in names)
                # External symlink preserved as a symlink
                assert any("external_link" in n for n in names)
                ext = [
                    m for m in tar.getmembers() if "external_link" in m.name
                ]
                assert len(ext) == 1
                assert ext[0].issym()
        finally:
            run(["klangk", "rm", "e2e-symlink"], env=env)


class TestExportImport:
    @pytest.fixture(autouse=True)
    @staticmethod
    def _login(cli_config):
        """Ensure logged in for this test class."""
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        yield
        # No explicit workspace cleanup here — the shared server fixture
        # tears down the entire server process (and its data dir) after
        # the test session, so leftover workspaces are harmless.  Previous
        # attempts at per-test CLI cleanup caused cascade failures on CI
        # when `klangk rm` was slow (see #364).

    def test_export_and_import_round_trip(self, cli_config, tmp_path):
        env = cli_config["env"]

        # Create a workspace with metadata
        result = run(
            [
                "klangk",
                "create",
                "export-test",
                "--env",
                "MY_VAR=hello",
            ],
            env=env,
        )
        assert result.returncode == 0

        # Export (workspace has no container started yet — just metadata)
        archive = tmp_path / "export-test.tar.gz"
        result = run(
            ["klangk", "export", "export-test", "-o", str(archive)],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert archive.exists()
        assert archive.stat().st_size > 0

        # Verify archive contents
        import json
        import tarfile

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            assert "workspace.json" in names
            meta = json.loads(tar.extractfile("workspace.json").read())
            assert meta["name"] == "export-test"
            assert meta["env"] == {"MY_VAR": "hello"}

        # Delete the original (not needed for import, but keeps things tidy)
        try:
            run(["klangk", "rm", "export-test"], env=env)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Timeout removing export-test, deferring to teardown"
            )

        # Import with a new name
        result = run(
            [
                "klangk",
                "import",
                str(archive),
                "--name",
                "export-restored",
            ],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr or result.stdout

        # Verify the imported workspace exists
        result = run(["klangk", "ls", "--plain"], env=env)
        assert "export-restored" in result.stdout

    def test_export_import_round_trip_with_symlinks(
        self, server, cli_config, tmp_path
    ):
        """Symlinks survive an export→import round-trip intact."""
        env = cli_config["env"]

        result = run(["klangk", "create", "export-symlink"], env=env)
        assert result.returncode == 0

        try:
            # Find home dir on host
            from pathlib import Path

            import httpx

            resp = httpx.post(
                f"{server['url']}/api/v1/auth/login",
                json={
                    "identifier": "test@example.com",
                    "password": "testpass",
                },
                timeout=30,
            )
            token = resp.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            resp = httpx.get(
                f"{server['url']}/api/v1/workspaces",
                headers=headers,
                timeout=30,
            )
            ws = [w for w in resp.json() if w["name"] == "export-symlink"][0]
            ws_id = ws["id"]
            ws_root = Path(server["data_dir"]) / "workspaces"
            home_dir = ws_root / ws_id / "home"
            assert home_dir.exists(), f"workspace home dir missing: {home_dir}"

            # Create files and symlinks
            home_dir.mkdir(parents=True, exist_ok=True)
            (home_dir / "real.txt").write_text("original content")
            (home_dir / "relative_link").symlink_to("real.txt")
            (home_dir / "external_link").symlink_to("/etc/hostname")
            (home_dir / "container_link").symlink_to(
                "/home/klangk/.local/bin/test"
            )

            # Export
            archive = tmp_path / "symlink-roundtrip.tar.gz"
            result = run(
                ["klangk", "export", "export-symlink", "-o", str(archive)],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr or result.stdout

            # Verify archive has all symlinks
            import tarfile

            with tarfile.open(archive, "r:gz") as tar:
                names = tar.getnames()
                assert any("real.txt" in n for n in names)
                assert any("relative_link" in n for n in names)
                assert any("external_link" in n for n in names)
                assert any("container_link" in n for n in names)

                # Verify they are stored as symlinks, not regular files
                for link_name in [
                    "relative_link",
                    "external_link",
                    "container_link",
                ]:
                    members = [
                        m for m in tar.getmembers() if link_name in m.name
                    ]
                    assert len(members) == 1, f"{link_name} not found"
                    assert members[0].issym(), f"{link_name} not a symlink"

            # Delete original (not required for import — uses a different name)
            try:
                run(["klangk", "rm", "export-symlink"], env=env)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Timeout removing export-symlink, deferring to teardown"
                )

            result = run(
                [
                    "klangk",
                    "import",
                    str(archive),
                    "--name",
                    "export-symlink-imported",
                ],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr or result.stdout

            # Find the imported workspace's home dir
            resp = httpx.get(
                f"{server['url']}/api/v1/workspaces",
                headers=headers,
                timeout=30,
            )
            imported = [
                w
                for w in resp.json()
                if w["name"] == "export-symlink-imported"
            ][0]
            imported_home = ws_root / imported["id"] / "home"

            # Verify files and symlinks survived
            assert (
                imported_home / "real.txt"
            ).read_text() == "original content"
            assert (imported_home / "relative_link").is_symlink()
            assert (
                os.readlink(str(imported_home / "relative_link")) == "real.txt"
            )
            assert (imported_home / "external_link").is_symlink()
            assert (
                os.readlink(str(imported_home / "external_link"))
                == "/etc/hostname"
            )
            assert (imported_home / "container_link").is_symlink()
            assert (
                os.readlink(str(imported_home / "container_link"))
                == "/home/klangk/.local/bin/test"
            )

            try:
                run(["klangk", "rm", "export-symlink-imported"], env=env)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Timeout removing export-symlink-imported, deferring to teardown"
                )
        finally:
            try:
                run(["klangk", "rm", "export-symlink"], env=env)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Timeout removing export-symlink in finally, deferring to teardown"
                )


class TestAllowedMountRoots:
    """Verify KLANGKD_ALLOWED_MOUNT_ROOTS restricts bind mount sources."""

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def restricted_server(tmp_path_factory, request):
        data_dir = tracked_mkdtemp("klangk-mount-roots-")
        proc, base_url = _start_server(
            data_dir,
            str(free_port()),
            extra_env={"KLANGKD_ALLOWED_MOUNT_ROOTS": "/tmp,/home"},
        )
        config_dir = tmp_path_factory.mktemp("klangk-mount-roots-config")
        env = clean_env(HOME=str(config_dir))
        klangk_config_dir = config_dir / ".config" / "klangk"
        klangk_config_dir.mkdir(parents=True)
        run(
            [
                "klangk",
                "login",
                base_url,
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        request.cls._env = env
        request.cls._base_url = base_url
        yield
        _stop_server(proc, data_dir)

    def test_allowed_mount_succeeds(self):
        env = self._env
        try:
            result = run(
                [
                    "klangk",
                    "create",
                    "e2e-mount-ok",
                    "--mount",
                    "/tmp:/mnt/tmp",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-mount-ok" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-mount-ok"], env=env)

    def test_denied_mount_fails(self):
        env = self._env
        result = run(
            [
                "klangk",
                "create",
                "e2e-mount-denied",
                "--mount",
                "/etc/passwd:/secrets:ro",
            ],
            env=env,
        )
        assert result.returncode != 0
        assert (
            "allowed root" in result.stderr.lower()
            or "allowed root" in result.stdout.lower()
        )


class TestVolumeUserIsolation:
    """Volume ownership is isolated at runtime; management is admin-only.

    Mounting another user's named volume is rejected at container
    assembly (``ensure_volumes``, no HTTP ACL involved), while the
    ``volumes`` CLI surface — list, create, delete — is the #2993
    admin surface: seeded Allow for the admins group only, so a plain
    registered user is denied on every command.
    """

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def two_user_server(tmp_path_factory, request):
        import httpx

        data_dir = tracked_mkdtemp("klangk-vol-iso-")
        proc, base_url = _start_server(data_dir, str(free_port()))

        # Register a second user via the API
        httpx.post(
            f"{base_url}/api/v1/auth/register",
            json={
                "email": "user2@example.com",
                "password": "testpass2",
            },
        )

        # Set up CLI configs for both users
        for attr, email, password in [
            ("_env_a", "test@example.com", "testpass"),
            ("_env_b", "user2@example.com", "testpass2"),
        ]:
            config_dir = tmp_path_factory.mktemp(f"klangk-vol-iso-{attr}")
            env = clean_env(HOME=str(config_dir))
            (config_dir / ".config" / "klangk").mkdir(parents=True)
            run(
                [
                    "klangk",
                    "login",
                    base_url,
                    email,
                    "--password-file",
                    "-",
                ],
                input=f"{password}\n",
                env=env,
            )
            setattr(request.cls, attr, env)

        request.cls._base_url = base_url
        yield
        _stop_server(proc, data_dir)

    def test_cross_user_volume_rejected(self):
        env_a = self._env_a
        env_b = self._env_b

        # User A creates workspace with a named volume
        run(
            [
                "klangk",
                "create",
                "ws-a",
                "--mount",
                "shared-vol:/data",
            ],
            env=env_a,
        )
        try:
            # User A execs to trigger container start (creates the volume)
            result = run(
                ["klangk", "exec", "ws-a", "echo", "ok"],
                env=env_a,
                timeout=120,
            )
            assert result.returncode == 0

            # User B creates workspace with the same volume
            run(
                [
                    "klangk",
                    "create",
                    "ws-b",
                    "--mount",
                    "shared-vol:/data",
                ],
                env=env_b,
            )
            try:
                # User B execs — should fail because the volume belongs to A
                result = run(
                    ["klangk", "exec", "ws-b", "echo", "stolen"],
                    env=env_b,
                    timeout=120,
                )
                assert result.returncode != 0
            finally:
                run(["klangk", "rm", "ws-b"], env=env_b)
        finally:
            run(["klangk", "rm", "ws-a"], env=env_a)
            subprocess.run(
                ["podman", "volume", "rm", "shared-vol"],
                capture_output=True,
            )

    def test_volumes_non_admin_denied(self):
        """A plain registered user is denied the whole volumes surface
        (#2993): list, create, and delete all fail with a nonzero
        exit."""
        env_b = self._env_b

        result = run(["klangk", "volumes", "create", "vol-b"], env=env_b)
        assert result.returncode != 0
        assert "403" in result.stderr or "denied" in result.stderr.lower()

        result = run(["klangk", "volumes", "ls", "--plain"], env=env_b)
        assert result.returncode != 0
        assert "403" in result.stderr or "denied" in result.stderr.lower()

        # rm has an explicit 403 path: a clean 'Permission denied' error.
        result = run(["klangk", "volumes", "rm", "vol-b"], env=env_b)
        assert result.returncode != 0
        assert "Permission denied" in result.stderr

    def test_volumes_admin_manages_all_volumes(self):
        """The admin holds the whole surface: create, list (the whole
        instance inventory), and delete — including deleting a volume
        while a non-admin is refused on the same name (#2993)."""
        env_a = self._env_a
        env_b = self._env_b

        result = run(["klangk", "volumes", "create", "vol-a"], env=env_a)
        assert result.returncode == 0
        try:
            # The non-admin cannot delete what the admin created.
            result = run(["klangk", "volumes", "rm", "vol-a"], env=env_b)
            assert result.returncode != 0
            assert "Permission denied" in result.stderr

            # The admin lists the instance inventory and deletes from it.
            result = run(["klangk", "volumes", "ls", "--plain"], env=env_a)
            assert result.returncode == 0
            assert "vol-a" in result.stdout
        finally:
            run(["klangk", "volumes", "rm", "vol-a"], env=env_a)

        result = run(["klangk", "volumes", "ls", "--plain"], env=env_a)
        assert result.returncode == 0
        assert "vol-a" not in result.stdout


class TestTerminalSharing:
    """Test klangk terminals, share, and unshare commands.

    Uses its own dedicated server to avoid cascading failures from
    other test classes that may leave the shared server unresponsive.
    """

    @pytest.fixture(autouse=True, scope="class")
    @staticmethod
    def _dedicated_server(tmp_path_factory, request):
        data_dir = tracked_mkdtemp("klangk-terminal-sharing-")
        proc, base_url = _start_server(data_dir, str(free_port()))
        config_dir = tmp_path_factory.mktemp("klangk-terminal-sharing-config")
        env = clean_env(HOME=str(config_dir))
        (config_dir / ".config" / "klangk").mkdir(parents=True)
        run(
            [
                "klangk",
                "login",
                base_url,
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        run(["klangk", "create", "e2e-share"], env=env)
        # Start container so terminal commands work
        run(
            ["klangk", "exec", "e2e-share", "true"],
            env=env,
            timeout=120,
        )
        request.cls._env = env
        yield
        run(["klangk", "rm", "e2e-share"], env=env)
        _stop_server(proc, data_dir)

    def test_terminals_lists_windows(self):
        result = run(
            ["klangk", "terminal", "ls", "e2e-share"],
            env=self._env,
            timeout=120,
        )
        assert result.returncode == 0

    def test_share_and_unshare_terminal(self):
        env = self._env
        # First discover the window name via `klangk terminals`
        list_result = run(
            ["klangk", "terminal", "ls", "e2e-share"],
            env=env,
            timeout=120,
        )
        assert list_result.returncode == 0
        # Parse the Rich table to find the first "own" terminal's id.
        # Columns are ID, Name, Type, Owner, so an own-terminal row yields
        # filtered parts like ['@1', 'bash', 'own']. Target the window by
        # its @N id since names may duplicate (#2192).
        terminal_id = None
        for line in list_result.stderr.splitlines():
            if "│" in line and "own" in line:
                parts = [p.strip() for p in line.split("│")]
                parts = [p for p in parts if p]
                if len(parts) >= 3 and parts[2] == "own":
                    terminal_id = parts[0]
                    break
        assert terminal_id is not None, (
            f"Could not find terminal in output: {list_result.stderr}"
        )

        result = run(
            ["klangk", "terminal", "share", "e2e-share", terminal_id],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "shared" in result.stderr.lower()

        result = run(
            ["klangk", "terminal", "unshare", "e2e-share", terminal_id],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "no longer shared" in result.stderr.lower()

    def test_share_nonexistent_terminal(self):
        result = run(
            ["klangk", "terminal", "share", "e2e-share", "nonexistent"],
            env=self._env,
            timeout=120,
        )
        assert result.returncode != 0


class TestContainerReplace:
    """Verify podman --replace handles stale/crashed containers."""

    @pytest.fixture(autouse=True)
    @staticmethod
    def _login(cli_config):
        """Ensure logged in for this test class.

        TestLogout (scheduled anywhere under xdist) wipes the server entry
        from the session-shared CLI config, so any class WITHOUT its own
        re-login fails with "no server configured" when it lands after it
        on the same worker. Same pattern as TestExportSymlinks et al.
        """
        run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )

    def test_exec_after_external_stop(self, cli_config):
        """Kill a workspace container externally, then exec again.

        The backend's ``podman create --replace`` must replace the
        stopped container so the next exec succeeds.
        """
        env = cli_config["env"]
        run(["klangk", "create", "e2e-replace"], env=env)
        try:
            # Start the container via exec
            result = run(
                ["klangk", "exec", "e2e-replace", "echo", "first"],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0
            assert "first" in result.stdout

            # Kill the container externally (simulates crash)
            ps = subprocess.run(
                [
                    "podman",
                    "ps",
                    "-a",
                    "--filter",
                    "label=klangk.instance=cli-e2e",
                    "--filter",
                    "label=klangk.workspace",
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                text=True,
            )
            for cid in ps.stdout.strip().splitlines():
                subprocess.run(
                    ["podman", "stop", "-t", "0", cid],
                    capture_output=True,
                )

            # Exec again — --replace should create a fresh container
            result = run(
                ["klangk", "exec", "e2e-replace", "echo", "second"],
                env=env,
                timeout=120,
            )
            assert result.returncode == 0
            assert "second" in result.stdout
        finally:
            run(["klangk", "rm", "e2e-replace"], env=env)


class TestWorkspaceSharing:
    """Test klangk share/unshare/members commands."""

    @pytest.fixture(scope="class", autouse=True)
    @staticmethod
    def setup(server, tmp_path_factory, request):
        base_url = server["url"]

        # Register a second user
        import httpx

        httpx.post(
            f"{base_url}/api/v1/auth/register",
            json={
                "email": "share-user@example.com",
                "password": "testpass",
            },
        )

        config_dir = tmp_path_factory.mktemp("klangk-ws-share")
        env = clean_env(HOME=str(config_dir))
        (config_dir / ".config" / "klangk").mkdir(parents=True)
        run(
            [
                "klangk",
                "login",
                base_url,
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        run(["klangk", "create", "e2e-ws-share"], env=env)
        request.cls._env = env

    @pytest.fixture(autouse=True)
    def env(self):
        self.env = self.__class__._env

    def test_share_and_unshare_workspace(self):
        env = self.env

        # Share workspace with second user
        result = run(
            ["klangk", "share", "e2e-ws-share", "share-user@example.com"],
            env=env,
        )
        assert result.returncode == 0
        assert "share-user@example.com" in result.stdout

        # List members — should include the shared user
        result = run(
            ["klangk", "members", "e2e-ws-share"],
            env=env,
        )
        assert result.returncode == 0
        assert "share-user@example.com" in result.stdout

        # Unshare
        result = run(
            [
                "klangk",
                "unshare",
                "e2e-ws-share",
                "share-user@example.com",
            ],
            env=env,
        )
        assert result.returncode == 0

        # Shared user should be gone (owner may still appear)
        result = run(
            ["klangk", "members", "e2e-ws-share"],
            env=env,
        )
        assert result.returncode == 0
        assert "share-user@example.com" not in result.stdout

    def test_share_with_role(self):
        env = self.env

        # Share as spectator
        result = run(
            [
                "klangk",
                "share",
                "e2e-ws-share",
                "share-user@example.com",
                "--role=spectator",
            ],
            env=env,
        )
        assert result.returncode == 0
        assert "spectator" in result.stdout

        # Members should show spectator role
        result = run(
            ["klangk", "members", "e2e-ws-share"],
            env=env,
        )
        assert result.returncode == 0
        assert "spectator" in result.stdout

        # Change role to collaborator
        result = run(
            [
                "klangk",
                "share",
                "e2e-ws-share",
                "share-user@example.com",
                "--role=collaborator",
            ],
            env=env,
        )
        assert result.returncode == 0
        assert "collaborator" in result.stdout

        # Members should now show collaborator
        result = run(
            ["klangk", "members", "e2e-ws-share"],
            env=env,
        )
        assert result.returncode == 0
        assert "collaborator" in result.stdout
        assert "spectator" not in result.stdout

        # Cleanup
        run(
            [
                "klangk",
                "unshare",
                "e2e-ws-share",
                "share-user@example.com",
            ],
            env=env,
        )


# --- Token refresh E2E ---


@pytest.fixture(scope="module")
def short_token_server():
    """Start a server with very short token lifetime (~7 seconds)."""
    data_dir = tracked_mkdtemp("klangk-refresh-e2e-")
    proc, base_url = _start_server(
        data_dir,
        str(free_port()),
        extra_env={"KLANGKD_ACCESS_TOKEN_HOURS": "0.002"},
    )
    yield {"url": base_url, "data_dir": data_dir, "proc": proc}
    _stop_server(proc, data_dir)


@pytest.fixture(scope="module")
def short_token_cli_config(short_token_server, tmp_path_factory):
    """Create a CLI config pointing at the short-token server."""
    config_dir = tmp_path_factory.mktemp("klangk-refresh-config")
    env = clean_env(HOME=str(config_dir))
    klangk_config_dir = config_dir / ".config" / "klangk"
    klangk_config_dir.mkdir(parents=True)
    return {
        "env": env,
        "config_dir": klangk_config_dir,
        "server_url": short_token_server["url"],
    }


def _read_state_token(home_dir, server_url):
    """Read the stored token from klangk-state.yaml under *home_dir*.

    State lives under ``$XDG_STATE_HOME/klangk/`` (not the config tree) —
    #1646 split the CLI's config (``~/.config/klangk/``) from its state
    (``~/.local/state/klangk/``).
    """
    import yaml

    state_path = home_dir / ".local" / "state" / "klangk" / "klangk-state.yaml"
    if not state_path.exists():
        return None
    data = yaml.safe_load(state_path.read_text()) or {}
    server = data.get(server_url, {})
    active_user = server.get("active-user")
    if not active_user:
        return None
    users = server.get("users", {})
    return users.get(active_user, {}).get("token")


class TestTokenRefresh:
    """E2E tests for automatic JWT refresh.

    Uses a dedicated server with ~7-second token lifetime.
    """

    def _login(self, cli_config):
        result = run(
            [
                "klangk",
                "login",
                cli_config["server_url"],
                "test@example.com",
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        assert result.returncode == 0, result.stderr
        return result

    def test_proactive_refresh_on_http(
        self, short_token_server, short_token_cli_config
    ):
        """After token nears expiry, klangk ls should refresh transparently."""
        self._login(short_token_cli_config)
        original_token = _read_state_token(
            Path(short_token_cli_config["env"]["HOME"]),
            short_token_cli_config["server_url"],
        )
        assert original_token is not None

        # Wait for the token to be within the refresh margin.
        # Token lifetime is 0.002 hours = 7.2 seconds.
        # Refresh margin is 300 seconds, so the token is *already*
        # within the refresh window from the moment it's issued.
        # A simple ls should trigger proactive refresh.
        result = run(
            ["klangk", "ls"],
            env=short_token_cli_config["env"],
        )
        assert result.returncode == 0, result.stderr

        new_token = _read_state_token(
            Path(short_token_cli_config["env"]["HOME"]),
            short_token_cli_config["server_url"],
        )
        assert new_token is not None
        assert new_token != original_token

    def test_401_retry_refreshes_and_succeeds(
        self, short_token_server, short_token_cli_config
    ):
        """After token expires, 401 retry should refresh and succeed."""
        self._login(short_token_cli_config)
        original_token = _read_state_token(
            Path(short_token_cli_config["env"]["HOME"]),
            short_token_cli_config["server_url"],
        )

        # Wait for the token to fully expire (7.2 seconds + buffer)
        time.sleep(9)

        result = run(
            ["klangk", "ls"],
            env=short_token_cli_config["env"],
        )
        # The proactive refresh should have gotten a new token before
        # the request, so this should succeed. If the proactive refresh
        # also fails (expired), then the 401 retry kicks in — but the
        # backend refresh endpoint rejects expired tokens unless they
        # were previously refreshed. In that case the command should
        # fail with a clean error (no traceback).
        if result.returncode == 0:
            new_token = _read_state_token(
                Path(short_token_cli_config["env"]["HOME"]),
                short_token_cli_config["server_url"],
            )
            assert new_token != original_token
        else:
            # Clean error, no traceback
            assert "Traceback" not in result.stderr
            assert (
                "Session expired" in result.stderr
                or "login" in result.stderr.lower()
            )

    def test_token_persisted_after_refresh(
        self, short_token_server, short_token_cli_config
    ):
        """After a successful refresh, klangk-state.yaml has the new token."""
        self._login(short_token_cli_config)
        original_token = _read_state_token(
            Path(short_token_cli_config["env"]["HOME"]),
            short_token_cli_config["server_url"],
        )
        # The short lifetime means proactive refresh fires immediately
        result = run(
            ["klangk", "ls"],
            env=short_token_cli_config["env"],
        )
        assert result.returncode == 0, result.stderr
        new_token = _read_state_token(
            Path(short_token_cli_config["env"]["HOME"]),
            short_token_cli_config["server_url"],
        )
        assert new_token is not None
        assert new_token != original_token
