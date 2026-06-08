"""CLI end-to-end tests against a real Klangk server.

These tests start a real uvicorn server, run klangk CLI commands as
subprocesses, and verify behavior against real podman containers.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-cli-e2e
"""

import os
import shutil
import subprocess
import tempfile
import time

import pytest


def _run(args, timeout=30, input=None, **kwargs):
    """Run a CLI command, return CompletedProcess."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input,
        **kwargs,
    )


def _start_server(data_dir, port, instance_id, extra_env=None):
    """Start a Klangk server and wait for it to be ready.

    Returns (proc, base_url).
    """
    import httpx

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "cli-e2e-test-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": instance_id,
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": "9000",
        "LOGFIRE_TOKEN": "",
        **(extra_env or {}),
    }
    proc = subprocess.Popen(
        [
            "uvicorn",
            "klangk_backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--ws-max-size",
            "16777216",
            "--ws-ping-interval",
            "20",
            "--ws-ping-timeout",
            "20",
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")
    return proc, base_url


def _stop_server(proc, data_dir, instance_id):
    """Stop a server, clean up containers and data."""
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"label=klangk.instance={instance_id}",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["podman", "rm", "-f", *result.stdout.strip().split()],
            capture_output=True,
        )
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def server():
    """Start a real Klangk server for the test session."""
    data_dir = tempfile.mkdtemp(prefix="klangk-cli-e2e-")
    proc, base_url = _start_server(data_dir, "18995", "cli-e2e")
    yield {
        "url": base_url,
        "port": "18995",
        "data_dir": data_dir,
        "proc": proc,
    }
    _stop_server(proc, data_dir, "cli-e2e")


@pytest.fixture(scope="session")
def cli_config(server, tmp_path_factory):
    """Create a CLI config pointing at the test server."""
    config_dir = tmp_path_factory.mktemp("klangk-cli-config")
    env = {**os.environ, "HOME": str(config_dir)}
    # The CLI reads from ~/.config/klangk/cli.toml
    klangk_config_dir = config_dir / ".config" / "klangk"
    klangk_config_dir.mkdir(parents=True)
    return {
        "env": env,
        "config_dir": klangk_config_dir,
        "config_file": klangk_config_dir / "cli.toml",
        "server_url": server["url"],
    }


@pytest.fixture(autouse=True, scope="session")
def _ensure_login(cli_config):
    """Log in once for the entire test session.

    This allows any test class to run in isolation with -k without
    depending on TestLogin having run first.
    """
    _run(
        [
            "klangk",
            "login",
            "test@example.com",
            "--server",
            cli_config["server_url"],
            "--password-file",
            "-",
        ],
        input="testpass\n",
        env=cli_config["env"],
    )


class TestLogin:
    def test_login_with_email_arg(self, server, cli_config):
        result = _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                server["url"],
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
        result = _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                server["url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Already logged in" in result.stdout

    def test_status_shows_logged_in(self, cli_config):
        result = _run(
            ["klangk", "status", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "status=logged_in" in result.stdout
        assert "test@example.com" in result.stdout


class TestWorkspaceCRUD:
    def test_create_workspace(self, cli_config):
        result = _run(
            ["klangk", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_list_workspaces(self, cli_config):
        result = _run(
            ["klangk", "list", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_create_duplicate_fails(self, cli_config):
        result = _run(
            ["klangk", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_nonexistent_fails(self, cli_config):
        result = _run(
            ["klangk", "rm", "nonexistent-ws"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_workspace(self, cli_config):
        result = _run(
            ["klangk", "rm", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Deleted" in result.stdout

    def test_list_after_delete(self, cli_config):
        result = _run(
            ["klangk", "list", "--plain"],
            env=cli_config["env"],
        )
        assert "e2e-crud" not in result.stdout


class TestDuplicate:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_dup_workspace(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        _run(
            ["klangk", "create", "e2e-dup-src", "--env", "FOO=bar"],
            env=env,
        )
        try:
            result = _run(
                ["klangk", "dup", "e2e-dup-src", "e2e-dup-copy"],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-dup-copy" in result.stdout

            # Verify copy appears in list
            result = _run(
                ["klangk", "list", "--plain"],
                env=env,
            )
            assert "e2e-dup-src" in result.stdout
            assert "e2e-dup-copy" in result.stdout
        finally:
            _run(["klangk", "rm", "e2e-dup-copy"], env=env)
            _run(["klangk", "rm", "e2e-dup-src"], env=env)

    def test_dup_nonexistent(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        result = _run(
            ["klangk", "dup", "no-such-ws", "copy"],
            env=env,
        )
        assert result.returncode != 0


class TestExec:
    @pytest.fixture(autouse=True, scope="class")
    def workspace(self, cli_config):
        _run(["klangk", "create", "e2e-exec"], env=cli_config["env"])
        yield
        _run(["klangk", "rm", "e2e-exec"], env=cli_config["env"])

    def test_exec_echo(self, cli_config):
        result = _run(
            ["klangk", "exec", "e2e-exec", "echo", "hello from exec"],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert "hello from exec" in result.stdout

    def test_exec_piped_stdin(self, cli_config):
        result = _run(
            ["klangk", "exec", "e2e-exec", "cat"],
            input="piped data\n",
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert "piped data" in result.stdout

    def test_exec_exit_code(self, cli_config):
        result = _run(
            ["klangk", "exec", "e2e-exec", "false"],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode != 0

    def test_exec_yes_backpressure(self, cli_config):
        """Smoke test: run `yes` briefly to exercise bounded queue back-pressure."""
        result = _run(
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
    def workspace(self, cli_config):
        _run(["klangk", "create", "e2e-sync"], env=cli_config["env"])
        yield
        _run(["klangk", "rm", "e2e-sync"], env=cli_config["env"])

    def test_sync_to_container(self, cli_config, tmp_path):
        # Create local files
        src = tmp_path / "sync-src"
        src.mkdir()
        (src / "file1.txt").write_text("content one")
        (src / "file2.txt").write_text("content two")

        result = _run(
            [
                "klangk",
                "sync",
                str(src) + "/",
                "e2e-sync:/home/klangk/work/synced/",
            ],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0

        # Verify files arrived
        verify = _run(
            [
                "klangk",
                "exec",
                "e2e-sync",
                "cat",
                "/home/klangk/work/synced/file1.txt",
            ],
            env=cli_config["env"],
            timeout=60,
        )
        assert verify.returncode == 0
        assert "content one" in verify.stdout

    def test_sync_from_container(self, cli_config, tmp_path):
        # Create a file in the container
        _run(
            [
                "klangk",
                "exec",
                "e2e-sync",
                "bash",
                "-c",
                "echo remote-data > /home/klangk/work/remote-file.txt",
            ],
            env=cli_config["env"],
            timeout=60,
        )

        dest = tmp_path / "sync-dest"
        dest.mkdir()

        result = _run(
            [
                "klangk",
                "sync",
                "e2e-sync:/home/klangk/work/remote-file.txt",
                str(dest) + "/",
            ],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert (dest / "remote-file.txt").read_text().strip() == "remote-data"


class TestSyncLarge:
    """Test syncing directories with 10+ MB of data."""

    @pytest.fixture(autouse=True, scope="class")
    def workspace(self, cli_config):
        _run(["klangk", "create", "e2e-sync-large"], env=cli_config["env"])
        yield
        _run(["klangk", "rm", "e2e-sync-large"], env=cli_config["env"])

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
        result = _run(
            [
                "klangk",
                "sync",
                str(src) + "/",
                "e2e-sync-large:/home/klangk/work/large-upload/",
            ],
            env=env,
            timeout=120,
        )
        assert result.returncode == 0

        # Verify file count in container
        verify = _run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "find /home/klangk/work/large-upload -type f | wc -l",
            ],
            env=env,
            timeout=60,
        )
        assert verify.returncode == 0
        assert int(verify.stdout.strip()) == file_count

        # Verify total size in container
        verify = _run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "du -sb /home/klangk/work/large-upload | cut -f1",
            ],
            env=env,
            timeout=60,
        )
        assert verify.returncode == 0
        container_size = int(verify.stdout.strip())
        assert container_size >= 10 * 1024 * 1024

        # Spot-check a few file hashes via exec
        for rel, expected_hash in list(src_hashes.items())[:3]:
            verify = _run(
                [
                    "klangk",
                    "exec",
                    "e2e-sync-large",
                    "sha256sum",
                    f"/home/klangk/work/large-upload/{rel}",
                ],
                env=env,
                timeout=60,
            )
            assert verify.returncode == 0
            assert expected_hash in verify.stdout

    def test_sync_large_from_container(self, cli_config, tmp_path):
        import hashlib

        env = cli_config["env"]

        # Create large data in the container
        _run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "mkdir -p /home/klangk/work/large-download && "
                "for i in $(seq 1 25); do "
                "dd if=/dev/urandom of=/home/klangk/work/large-download/file$i.bin "
                "bs=1024 count=420 status=none; done",
            ],
            env=env,
            timeout=60,
        )

        # Verify size in container (~10.5 MB)
        verify = _run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "du -sb /home/klangk/work/large-download | cut -f1",
            ],
            env=env,
            timeout=60,
        )
        assert verify.returncode == 0
        assert int(verify.stdout.strip()) >= 10 * 1024 * 1024

        # Get hashes in container
        verify = _run(
            [
                "klangk",
                "exec",
                "e2e-sync-large",
                "bash",
                "-c",
                "sha256sum /home/klangk/work/large-download/*.bin",
            ],
            env=env,
            timeout=60,
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

        result = _run(
            [
                "klangk",
                "sync",
                "e2e-sync-large:/home/klangk/work/large-download/",
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


class TestDefaultCommand:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_default_command_written_to_container(self, cli_config):
        """set-command → container gets KLANGK_DEFAULT_COMMAND → .klangk-command."""
        env = cli_config["env"]
        self._login(cli_config)
        _run(["klangk", "create", "e2e-defcmd"], env=env)
        try:
            # Set command before container starts
            result = _run(
                ["klangk", "edit", "e2e-defcmd", "--command", "echo hello"],
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout

            # exec triggers container start; config mount has the command
            result = _run(
                [
                    "klangk",
                    "exec",
                    "e2e-defcmd",
                    "cat",
                    "/opt/klangk/config/default-command",
                ],
                env=env,
                timeout=60,
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "echo hello"

            # Clear
            result = _run(
                ["klangk", "edit", "e2e-defcmd", "--command", ""], env=env
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            _run(["klangk", "rm", "e2e-defcmd"], env=env)

    def test_default_command_bash_no_infinite_loop(self, cli_config):
        """Setting default command to bash should not cause infinite recursion."""
        env = cli_config["env"]
        self._login(cli_config)
        _run(["klangk", "create", "e2e-defbash"], env=env)
        try:
            _run(
                ["klangk", "edit", "e2e-defbash", "--command", "bash"],
                env=env,
            )
            # Start the container first
            _run(
                ["klangk", "exec", "e2e-defbash", "true"],
                env=env,
                timeout=30,
            )
            # Run an interactive bash inside the container that sources
            # .bashrc, which would exec bash again without the
            # KLANGK_CMD_STARTED guard. If recursion happens, this hangs
            # and times out. We pipe "exit" to terminate the shell.
            result = _run(
                [
                    "klangk",
                    "exec",
                    "e2e-defbash",
                    "bash",
                    "-ic",
                    "exit 0",
                ],
                env=env,
                timeout=15,
            )
            assert result.returncode == 0
        finally:
            _run(["klangk", "rm", "e2e-defbash"], env=env)


class TestMounts:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_create_with_mount_flag(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        try:
            result = _run(
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
            _run(["klangk", "rm", "e2e-mount"], env=env)

    def test_edit_with_mount_flags(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        _run(["klangk", "create", "e2e-mount-edit"], env=env)
        try:
            result = _run(
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
            _run(["klangk", "rm", "e2e-mount-edit"], env=env)

    def test_edit_interactive_add_mount(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        _run(["klangk", "create", "e2e-mount-int"], env=env)
        try:
            # Interactive: keep name, keep image, keep command,
            # add mount "/tmp:/mnt/test", skip add, skip remove,
            # skip add env
            result = _run(
                ["klangk", "edit", "e2e-mount-int"],
                input="\n\n\n/tmp:/mnt/test\n\n\n\n",
                env=env,
            )
            assert result.returncode == 0
            assert "Updated" in result.stdout
        finally:
            _run(["klangk", "rm", "e2e-mount-int"], env=env)


class TestEnvVars:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_create_with_env_flag(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        try:
            result = _run(
                [
                    "klangk",
                    "create",
                    "e2e-env",
                    "--env",
                    "FOO=bar",
                    "--env",
                    "KLANGK_SKILLS=test",
                ],
                env=env,
            )
            assert result.returncode == 0
            assert "e2e-env" in result.stdout
        finally:
            _run(["klangk", "rm", "e2e-env"], env=env)

    def test_edit_with_env_flag(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        _run(["klangk", "create", "e2e-env-edit"], env=env)
        try:
            result = _run(
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
            _run(["klangk", "rm", "e2e-env-edit"], env=env)


class TestVolumes:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )

    def test_volumes_lifecycle(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)

        # Create
        result = _run(["klangk", "volumes", "create", "e2e-vol"], env=env)
        assert result.returncode == 0
        assert "Created" in result.stdout

        # List
        result = _run(["klangk", "volumes", "ls", "--plain"], env=env)
        assert result.returncode == 0
        assert "e2e-vol" in result.stdout

        # Create duplicate fails
        result = _run(["klangk", "volumes", "create", "e2e-vol"], env=env)
        assert result.returncode != 0

        # Remove
        result = _run(["klangk", "volumes", "rm", "e2e-vol"], env=env)
        assert result.returncode == 0
        assert "Deleted" in result.stdout

        # List after delete
        result = _run(["klangk", "volumes", "ls", "--plain"], env=env)
        assert "e2e-vol" not in result.stdout

    def test_volumes_rm_nonexistent(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        result = _run(["klangk", "volumes", "rm", "no-such-vol"], env=env)
        assert result.returncode != 0

    def test_volumes_empty_list(self, cli_config):
        env = cli_config["env"]
        self._login(cli_config)
        result = _run(["klangk", "volumes", "ls"], env=env)
        assert result.returncode == 0
        # May show "No volumes." or an empty table


class TestAuthError:
    def test_command_without_login_shows_clean_error(self, server, tmp_path):
        """Commands that need auth should show a clean error, not a traceback."""
        # Fresh config dir with no login
        config_dir = tmp_path / "no-login"
        config_dir.mkdir()
        klangk_config = config_dir / ".config" / "klangk"
        klangk_config.mkdir(parents=True)
        env = {**os.environ, "HOME": str(config_dir)}
        result = _run(
            ["klangk", "list"],
            env=env,
        )
        assert result.returncode != 0
        assert "Traceback" not in result.stderr
        assert "login" in result.stderr.lower()


class TestLogout:
    def test_logout(self, cli_config):
        result = _run(
            ["klangk", "logout"],
            env=cli_config["env"],
        )
        assert result.returncode == 0

    def test_status_after_logout(self, cli_config):
        result = _run(
            ["klangk", "status", "--plain"],
            env=cli_config["env"],
        )
        assert "not_logged_in" in result.stdout


class TestExportSymlinks:
    @pytest.fixture(autouse=True)
    def _login(self, cli_config):
        """Ensure logged in for this test class."""
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
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
            f"{server['url']}/auth/login",
            json={"email": "test@example.com", "password": "testpass"},
        )
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = httpx.get(f"{server['url']}/workspaces", headers=headers)
        ws = [w for w in resp.json() if w["name"] == "e2e-symlink"][0]
        ws_id = ws["id"]

        # Find the user directory — it's the only subdirectory of workspaces/
        ws_root = Path(server["data_dir"]) / "workspaces"
        user_dirs = [d for d in ws_root.iterdir() if d.is_dir()]
        assert len(user_dirs) == 1
        return user_dirs[0] / "home" / ws_id

    def test_export_preserves_all_symlinks(self, server, cli_config, tmp_path):
        """All symlinks are preserved (stored as links, not content)."""
        env = cli_config["env"]

        result = _run(["klangk", "create", "e2e-symlink"], env=env)
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
            result = _run(
                ["klangk", "export", "e2e-symlink", "-o", str(archive)],
                env=env,
                timeout=60,
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
            _run(["klangk", "rm", "e2e-symlink"], env=env)


class TestExportImport:
    @pytest.fixture(autouse=True)
    def _login(self, cli_config):
        """Ensure logged in for this test class."""
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                cli_config["server_url"],
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=cli_config["env"],
        )
        yield
        # Clean up workspaces created during tests
        result = _run(["klangk", "list", "--plain"], env=cli_config["env"])
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if parts and parts[0].startswith("export-"):
                _run(["klangk", "rm", parts[0]], env=cli_config["env"])

    def test_export_and_import_round_trip(self, cli_config, tmp_path):
        env = cli_config["env"]

        # Create a workspace with metadata
        result = _run(
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
        result = _run(
            ["klangk", "export", "export-test", "-o", str(archive)],
            env=env,
            timeout=60,
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

        # Delete the original
        _run(["klangk", "rm", "export-test"], env=env)

        # Import with a new name
        result = _run(
            [
                "klangk",
                "import",
                str(archive),
                "--name",
                "export-restored",
            ],
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr or result.stdout

        # Verify the imported workspace exists
        result = _run(["klangk", "list", "--plain"], env=env)
        assert "export-restored" in result.stdout

        # Clean up
        _run(["klangk", "rm", "export-restored"], env=env)

    def test_export_import_round_trip_with_symlinks(
        self, server, cli_config, tmp_path
    ):
        """Symlinks survive an export→import round-trip intact."""
        env = cli_config["env"]

        result = _run(["klangk", "create", "export-symlink"], env=env)
        assert result.returncode == 0

        try:
            # Find home dir on host
            from pathlib import Path

            import httpx

            resp = httpx.post(
                f"{server['url']}/auth/login",
                json={"email": "test@example.com", "password": "testpass"},
            )
            token = resp.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            resp = httpx.get(f"{server['url']}/workspaces", headers=headers)
            ws = [w for w in resp.json() if w["name"] == "export-symlink"][0]
            ws_id = ws["id"]
            ws_root = Path(server["data_dir"]) / "workspaces"
            user_dirs = [d for d in ws_root.iterdir() if d.is_dir()]
            assert len(user_dirs) >= 1
            home_dir = user_dirs[0] / "home" / ws_id

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
            result = _run(
                ["klangk", "export", "export-symlink", "-o", str(archive)],
                env=env,
                timeout=60,
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

            # Delete original and import
            _run(["klangk", "rm", "export-symlink"], env=env)

            result = _run(
                [
                    "klangk",
                    "import",
                    str(archive),
                    "--name",
                    "export-symlink-imported",
                ],
                env=env,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr or result.stdout

            # Find the imported workspace's home dir
            resp = httpx.get(f"{server['url']}/workspaces", headers=headers)
            imported = [
                w
                for w in resp.json()
                if w["name"] == "export-symlink-imported"
            ][0]
            imported_home = user_dirs[0] / "home" / imported["id"]

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

            _run(["klangk", "rm", "export-symlink-imported"], env=env)
        finally:
            _run(["klangk", "rm", "export-symlink"], env=env)


class TestAllowedMountRoots:
    """Verify KLANGK_ALLOWED_MOUNT_ROOTS restricts bind mount sources."""

    @pytest.fixture(autouse=True, scope="class")
    def restricted_server(self, tmp_path_factory):
        data_dir = tempfile.mkdtemp(prefix="klangk-mount-roots-")
        proc, base_url = _start_server(
            data_dir,
            "18998",
            "mount-roots-e2e",
            extra_env={"KLANGK_ALLOWED_MOUNT_ROOTS": "/tmp,/home"},
        )
        config_dir = tmp_path_factory.mktemp("klangk-mount-roots-config")
        env = {**os.environ, "HOME": str(config_dir)}
        klangk_config_dir = config_dir / ".config" / "klangk"
        klangk_config_dir.mkdir(parents=True)
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                base_url,
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        self.__class__._env = env
        self.__class__._base_url = base_url
        yield
        _stop_server(proc, data_dir, "mount-roots-e2e")

    def test_allowed_mount_succeeds(self):
        env = self._env
        try:
            result = _run(
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
            _run(["klangk", "rm", "e2e-mount-ok"], env=env)

    def test_denied_mount_fails(self):
        env = self._env
        result = _run(
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
    """Verify that a user cannot mount another user's volume."""

    @pytest.fixture(autouse=True, scope="class")
    def two_user_server(self, tmp_path_factory):
        import httpx

        data_dir = tempfile.mkdtemp(prefix="klangk-vol-iso-")
        proc, base_url = _start_server(
            data_dir,
            "18999",
            "vol-iso-e2e",
        )

        # Register a second user via the API
        httpx.post(
            f"{base_url}/auth/register",
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
            env = {**os.environ, "HOME": str(config_dir)}
            (config_dir / ".config" / "klangk").mkdir(parents=True)
            _run(
                [
                    "klangk",
                    "login",
                    email,
                    "--server",
                    base_url,
                    "--password-file",
                    "-",
                ],
                input=f"{password}\n",
                env=env,
            )
            setattr(self.__class__, attr, env)

        self.__class__._base_url = base_url
        yield
        _stop_server(proc, data_dir, "vol-iso-e2e")

    def test_cross_user_volume_rejected(self):
        env_a = self._env_a
        env_b = self._env_b

        # User A creates workspace with a named volume
        _run(
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
            result = _run(
                ["klangk", "exec", "ws-a", "echo", "ok"],
                env=env_a,
                timeout=60,
            )
            assert result.returncode == 0

            # User B creates workspace with the same volume
            _run(
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
                result = _run(
                    ["klangk", "exec", "ws-b", "echo", "stolen"],
                    env=env_b,
                    timeout=60,
                )
                assert result.returncode != 0
            finally:
                _run(["klangk", "rm", "ws-b"], env=env_b)
        finally:
            _run(["klangk", "rm", "ws-a"], env=env_a)
            subprocess.run(
                ["podman", "volume", "rm", "shared-vol"],
                capture_output=True,
            )


class TestContainerReplace:
    """Verify podman --replace handles stale/crashed containers."""

    def test_exec_after_external_stop(self, cli_config):
        """Kill a workspace container externally, then exec again.

        The backend's ``podman create --replace`` must replace the
        stopped container so the next exec succeeds.
        """
        env = cli_config["env"]
        _run(["klangk", "create", "e2e-replace"], env=env)
        try:
            # Start the container via exec
            result = _run(
                ["klangk", "exec", "e2e-replace", "echo", "first"],
                env=env,
                timeout=60,
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
                    "label=klangk.workspace-id",
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
            result = _run(
                ["klangk", "exec", "e2e-replace", "echo", "second"],
                env=env,
                timeout=60,
            )
            assert result.returncode == 0
            assert "second" in result.stdout
        finally:
            _run(["klangk", "rm", "e2e-replace"], env=env)
