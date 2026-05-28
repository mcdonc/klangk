"""CLI end-to-end tests against a real Bark server.

These tests start a real uvicorn server, run bark CLI commands as
subprocesses, and verify behavior against real Docker containers.

Requires: Docker running, bark-pi image built.

Run with: devenv shell -- test-cli-e2e
"""

import os
import shutil
import signal
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


@pytest.fixture(scope="session")
def server():
    """Start a real Bark server for the test session."""
    data_dir = tempfile.mkdtemp(prefix="bark-cli-e2e-")
    port = "18995"
    env = {
        **os.environ,
        "BARK_PORT": port,
        "BARK_DATA_DIR": data_dir,
        "BARK_JWT_SECRET": "cli-e2e-test-secret",
        "BARK_DEFAULT_USER": "test@example.com",
        "BARK_DEFAULT_PASSWORD": "testpass",
        "BARK_TEST_MODE": "1",
        "BARK_INSTANCE_ID": "cli-e2e",
        "BARK_IDLE_TIMEOUT_SECONDS": "300",
        "BARK_PORT_RANGE_START": "9000",
        "LOGFIRE_TOKEN": "",
    }
    proc = subprocess.Popen(
        [
            "uvicorn",
            "bark_backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for server to be ready
    base_url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            import httpx

            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")

    yield {"url": base_url, "port": port, "data_dir": data_dir, "proc": proc}

    # Cleanup
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    # Clean up containers
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=bark.instance=cli-e2e",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["docker", "rm", "-f", *result.stdout.strip().split()],
            capture_output=True,
        )
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def cli_config(server, tmp_path_factory):
    """Create a CLI config pointing at the test server."""
    config_dir = tmp_path_factory.mktemp("bark-cli-config")
    env = {**os.environ, "HOME": str(config_dir)}
    # The CLI reads from ~/.config/bark/cli.toml
    bark_config_dir = config_dir / ".config" / "bark"
    bark_config_dir.mkdir(parents=True)
    return {
        "env": env,
        "config_dir": bark_config_dir,
        "config_file": bark_config_dir / "cli.toml",
        "server_url": server["url"],
    }


class TestLogin:
    def test_login_with_email_arg(self, server, cli_config):
        result = _run(
            [
                "bark",
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
                "bark",
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
            ["bark", "status", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "status=logged_in" in result.stdout
        assert "test@example.com" in result.stdout


class TestWorkspaceCRUD:
    def test_create_workspace(self, cli_config):
        result = _run(
            ["bark", "ws", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_list_workspaces(self, cli_config):
        result = _run(
            ["bark", "ws", "list", "--plain"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "e2e-crud" in result.stdout

    def test_create_duplicate_fails(self, cli_config):
        result = _run(
            ["bark", "ws", "create", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_nonexistent_fails(self, cli_config):
        result = _run(
            ["bark", "ws", "delete", "nonexistent-ws"],
            env=cli_config["env"],
        )
        assert result.returncode != 0

    def test_delete_workspace(self, cli_config):
        result = _run(
            ["bark", "ws", "delete", "e2e-crud"],
            env=cli_config["env"],
        )
        assert result.returncode == 0
        assert "Deleted" in result.stdout

    def test_list_after_delete(self, cli_config):
        result = _run(
            ["bark", "ws", "list", "--plain"],
            env=cli_config["env"],
        )
        assert "e2e-crud" not in result.stdout


class TestExec:
    @pytest.fixture(autouse=True, scope="class")
    def workspace(self, cli_config):
        _run(["bark", "ws", "create", "e2e-exec"], env=cli_config["env"])
        yield
        _run(["bark", "ws", "delete", "e2e-exec"], env=cli_config["env"])

    def test_exec_echo(self, cli_config):
        result = _run(
            ["bark", "ws", "exec", "e2e-exec", "echo", "hello from exec"],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert "hello from exec" in result.stdout

    def test_exec_piped_stdin(self, cli_config):
        result = _run(
            ["bark", "ws", "exec", "e2e-exec", "cat"],
            input="piped data\n",
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert "piped data" in result.stdout

    def test_exec_exit_code(self, cli_config):
        result = _run(
            ["bark", "ws", "exec", "e2e-exec", "false"],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode != 0


class TestSync:
    @pytest.fixture(autouse=True, scope="class")
    def workspace(self, cli_config):
        _run(["bark", "ws", "create", "e2e-sync"], env=cli_config["env"])
        yield
        _run(["bark", "ws", "delete", "e2e-sync"], env=cli_config["env"])

    def test_sync_to_container(self, cli_config, tmp_path):
        # Create local files
        src = tmp_path / "sync-src"
        src.mkdir()
        (src / "file1.txt").write_text("content one")
        (src / "file2.txt").write_text("content two")

        result = _run(
            [
                "bark",
                "ws",
                "sync",
                str(src) + "/",
                "e2e-sync:/work/synced/",
            ],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0

        # Verify files arrived
        verify = _run(
            [
                "bark",
                "ws",
                "exec",
                "e2e-sync",
                "cat",
                "/work/synced/file1.txt",
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
                "bark",
                "ws",
                "exec",
                "e2e-sync",
                "bash",
                "-c",
                "echo remote-data > /work/remote-file.txt",
            ],
            env=cli_config["env"],
            timeout=60,
        )

        dest = tmp_path / "sync-dest"
        dest.mkdir()

        result = _run(
            [
                "bark",
                "ws",
                "sync",
                "e2e-sync:/work/remote-file.txt",
                str(dest) + "/",
            ],
            env=cli_config["env"],
            timeout=60,
        )
        assert result.returncode == 0
        assert (dest / "remote-file.txt").read_text().strip() == "remote-data"


class TestDefaultCommand:
    def _login(self, cli_config):
        env = cli_config["env"]
        _run(
            [
                "bark",
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
        """set-command → container gets BARK_DEFAULT_COMMAND → .bark-command."""
        env = cli_config["env"]
        self._login(cli_config)
        _run(["bark", "ws", "create", "e2e-defcmd"], env=env)
        try:
            # Set command before container starts
            result = _run(
                ["bark", "ws", "set-command", "e2e-defcmd", "echo hello"],
                env=env,
            )
            assert result.returncode == 0
            assert "Default command set to" in result.stdout

            # exec triggers container start; config mount has the command
            result = _run(
                [
                    "bark",
                    "ws",
                    "exec",
                    "e2e-defcmd",
                    "cat",
                    "/opt/bark/config/default-command",
                ],
                env=env,
                timeout=60,
            )
            assert result.returncode == 0
            assert result.stdout.strip() == "echo hello"

            # Clear
            result = _run(["bark", "ws", "set-command", "e2e-defcmd"], env=env)
            assert result.returncode == 0
            assert "cleared" in result.stdout
        finally:
            _run(["bark", "ws", "delete", "e2e-defcmd"], env=env)

    def test_default_command_bash_no_infinite_loop(self, cli_config):
        """Setting default command to bash should not cause infinite recursion."""
        env = cli_config["env"]
        self._login(cli_config)
        _run(["bark", "ws", "create", "e2e-defbash"], env=env)
        try:
            _run(
                ["bark", "ws", "set-command", "e2e-defbash", "bash"],
                env=env,
            )
            # Start the container first
            _run(
                ["bark", "ws", "exec", "e2e-defbash", "true"],
                env=env,
                timeout=30,
            )
            # Run an interactive bash inside the container that sources
            # .bashrc, which would exec bash again without the
            # BARK_CMD_STARTED guard. If recursion happens, this hangs
            # and times out. We pipe "exit" to terminate the shell.
            result = _run(
                [
                    "bark",
                    "ws",
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
            _run(["bark", "ws", "delete", "e2e-defbash"], env=env)


class TestLogout:
    def test_logout(self, cli_config):
        result = _run(
            ["bark", "logout"],
            env=cli_config["env"],
        )
        assert result.returncode == 0

    def test_status_after_logout(self, cli_config):
        result = _run(
            ["bark", "status", "--plain"],
            env=cli_config["env"],
        )
        assert "not_logged_in" in result.stdout
