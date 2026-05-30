"""CLI end-to-end tests against a real Klangk server.

These tests start a real uvicorn server, run klangk CLI commands as
subprocesses, and verify behavior against real Docker containers.

Requires: Docker running, klangk image built.

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
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    result = subprocess.run(
        [
            "docker",
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
            ["docker", "rm", "-f", *result.stdout.strip().split()],
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


class TestOtelEnvVars:
    """Verify OTEL env vars are injected when LOGFIRE_TOKEN is set.

    Uses its own server with a fake LOGFIRE_TOKEN so the main test
    server isn't affected by OTEL exporter attempts.
    """

    @pytest.fixture(autouse=True)
    def otel_server(self, tmp_path):
        """Start a separate server with LOGFIRE_TOKEN set."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        proc, server_url = _start_server(
            data_dir,
            "18996",
            "cli-e2e-otel",
            extra_env={
                "LOGFIRE_TOKEN": "e2e-test-token",
                "KLANGK_PORT_RANGE_START": "9100",
            },
        )

        cli_env = {**os.environ, "HOME": str(tmp_path / "home")}
        (tmp_path / "home" / ".config" / "klangk").mkdir(parents=True)

        self._server_url = server_url
        self._env = cli_env
        yield
        _stop_server(proc, data_dir, "cli-e2e-otel")

    def test_otel_env_vars_in_container(self):
        env = self._env
        _run(
            [
                "klangk",
                "login",
                "test@example.com",
                "--server",
                self._server_url,
                "--password-file",
                "-",
            ],
            input="testpass\n",
            env=env,
        )
        _run(["klangk", "create", "e2e-otel"], env=env)
        try:
            result = _run(
                ["klangk", "exec", "e2e-otel", "env"],
                env=env,
                timeout=60,
            )
            assert result.returncode == 0
            container_env = dict(
                line.split("=", 1)
                for line in result.stdout.splitlines()
                if "=" in line
            )
            assert "OTEL_EXPORTER_OTLP_ENDPOINT" in container_env
            assert "OTEL_EXPORTER_OTLP_HEADERS" in container_env
            assert (
                "Authorization=Bearer e2e-test-token"
                in (container_env["OTEL_EXPORTER_OTLP_HEADERS"])
            )
            assert container_env["OTEL_SERVICE_NAME"] == "klangk-pi-agent"
        finally:
            _run(["klangk", "rm", "e2e-otel"], env=env)


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
