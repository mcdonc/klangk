"""Thin async wrapper around the ``podman`` CLI.

Drives a rootless, daemonless Podman engine with no socket.  Every call
shells out via ``asyncio.create_subprocess_exec`` and parses
``--format json``.

Non-zero exits raise :class:`PodmanError` whose ``status`` mimics
HTTP-like codes (404 not-found, 409 conflict/in-use) so callers can
branch accordingly; anything else maps to 500.

The binary is configurable via the ``podman_bin`` setting (defaults to
``podman``).
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncGenerator

from .settings import KlangkSettings, get_settings, resolve_indirection
from .util import BoundedOutputQueue

logger = logging.getLogger(__name__)


def _podman_bin(settings: KlangkSettings | None = None) -> str:
    return resolve_indirection((settings or get_settings()).podman_bin) or "podman"


def subprocess_env() -> dict[str, str]:
    """Return an environment dict for podman subprocesses.

    Strips ``LD_LIBRARY_PATH`` so the podman binary uses its own
    libraries.  Nix binaries have RPATH baked in and don't need it;
    system binaries (e.g. on CI) break if nix's glibc leaks in.
    """
    return {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}


class PodmanError(Exception):
    """A podman CLI invocation failed.

    ``status`` is an HTTP-like code (404, 409, 500) derived from stderr.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def classify(stderr: str) -> int:
    """Map podman stderr text to an HTTP-like status code."""
    low = stderr.lower()
    if "no such" in low or "not found" in low or "no container" in low:
        return 404
    if "in use" in low or "being used" in low or "already in use" in low:
        return 409
    return 500


async def run(
    args: list[str],
    *,
    check: bool = True,
    stdin_data: bytes | None = None,
    timeout: float | None = 30.0,
) -> tuple[int, str, str]:
    """Run ``podman <args>`` and return ``(returncode, stdout, stderr)``.

    Output is captured to temp files rather than ``stdout=PIPE`` +
    ``communicate()``.  Lifecycle commands such as ``podman start`` can
    spawn long-lived helpers (``pasta``) that inherit pipe fds, blocking
    ``communicate()`` forever.  Temp files avoid this.

    *timeout* caps how long we wait for the process (default 30 s).
    On timeout the process is killed and a ``PodmanError(500, ...)`` is
    raised (unless *check* is False, in which case rc=-1 is returned).
    """
    cmd_label = f"podman {args[0]}" if args else "podman"
    t0 = time.monotonic()
    with (
        tempfile.TemporaryFile() as out_f,
        tempfile.TemporaryFile() as err_f,
    ):
        t1 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            _podman_bin(),
            *args,
            stdin=(
                asyncio.subprocess.PIPE if stdin_data is not None else None
            ),
            stdout=out_f,
            stderr=err_f,
            env=subprocess_env(),
        )
        t2 = time.monotonic()
        if stdin_data is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "podman-timeout: %s exceeded %.1fs — killing",
                cmd_label,
                timeout,
            )
            proc.kill()
            await proc.wait()
        t3 = time.monotonic()
        out_f.seek(0)
        err_f.seek(0)
        out = out_f.read().decode("utf-8", errors="replace")
        err = err_f.read().decode("utf-8", errors="replace")
    rc = proc.returncode or 0
    if timed_out:
        rc = -1
        err = err or f"{cmd_label} timed out after {timeout}s"
    elapsed = t3 - t0
    logger.debug(
        "podman-timing: %s tempfile=%.3fs spawn=%.3fs wait=%.3fs total=%.3fs",
        cmd_label,
        t1 - t0,
        t2 - t1,
        t3 - t2,
        elapsed,
    )
    if elapsed > 2.0 and err.strip():  # pragma: no cover
        logger.debug("podman-timing: %s stderr: %s", cmd_label, err.strip())
    if check and rc != 0:
        raise PodmanError(classify(err), err.strip() or f"podman {args[0]}")
    return rc, out, err


async def run_raw(
    args: list[str],
    *,
    check: bool = True,
    stdin_data: bytes | None = None,
    timeout: float | None = 30.0,
) -> tuple[int, bytes, str]:
    """Like ``run`` but returns raw stdout bytes (for binary data)."""
    cmd_label = f"podman {args[0]}" if args else "podman"
    t0 = time.monotonic()
    with (
        tempfile.TemporaryFile() as out_f,
        tempfile.TemporaryFile() as err_f,
    ):
        t1 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            _podman_bin(),
            *args,
            stdin=(
                asyncio.subprocess.PIPE if stdin_data is not None else None
            ),
            stdout=out_f,
            stderr=err_f,
            env=subprocess_env(),
        )
        t2 = time.monotonic()
        if stdin_data is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "podman-timeout: %s exceeded %.1fs — killing",
                cmd_label,
                timeout,
            )
            proc.kill()
            await proc.wait()
        t3 = time.monotonic()
        out_f.seek(0)
        err_f.seek(0)
        out_bytes = out_f.read()
        err = err_f.read().decode("utf-8", errors="replace")
    rc = proc.returncode or 0
    if timed_out:
        rc = -1
        err = err or f"{cmd_label} timed out after {timeout}s"
    elapsed = t3 - t0
    logger.debug(
        "podman-timing: %s tempfile=%.3fs spawn=%.3fs wait=%.3fs total=%.3fs",
        cmd_label,
        t1 - t0,
        t2 - t1,
        t3 - t2,
        elapsed,
    )
    if elapsed > 2.0 and err.strip():  # pragma: no cover
        logger.debug("podman-timing: %s stderr: %s", cmd_label, err.strip())
    if check and rc != 0:
        raise PodmanError(classify(err), err.strip() or f"podman {args[0]}")
    return rc, out_bytes, err


# --- Containers ---


async def inspect_container(container_id: str) -> dict | None:
    """Return the inspect dict for a container, or None if it is gone."""
    rc, out, _err = await run(
        ["container", "inspect", container_id], check=False
    )
    if rc != 0:
        return None
    data = json.loads(out)
    return data[0] if data else None


async def create_container(
    name: str,
    image: str,
    *,
    labels: dict[str, str] | None = None,
    binds: list[str] | None = None,
    tmpfs: dict[str, str] | None = None,
    publish: list[tuple[int, int]] | None = None,
    add_hosts: list[str] | None = None,
    dns: list[str] | None = None,
    env: list[str] | None = None,
    init: bool = False,
    interactive: bool = False,
    pull: str = "never",
    replace: bool = True,
    userns: str | None = None,
) -> str:
    """Create a container and return its id.

    ``publish`` is a list of ``(host_port, container_port)`` pairs.
    ``replace=True`` removes an existing container with the same name.
    """
    args = ["create", f"--pull={pull}", "--name", name]
    if replace:
        args.append("--replace")
    if init:
        args.append("--init")
    if interactive:
        args.append("-i")
    if userns:
        args += ["--userns", userns]
    for key, value in (labels or {}).items():
        args += ["--label", f"{key}={value}"]
    for bind in binds or []:
        args += ["-v", bind]
    for path, opts in (tmpfs or {}).items():
        args += ["--tmpfs", f"{path}:{opts}"]
    for host_port, container_port in publish or []:
        args += ["-p", f"{host_port}:{container_port}"]
    for host in add_hosts or []:
        args += ["--add-host", host]
    for server in dns or []:
        args += ["--dns", server]
    for entry in env or []:
        args += ["-e", entry]
    args.append(image)
    _rc, out, _err = await run(args, timeout=120.0)
    return out.strip()


async def start_container(container_id: str) -> None:
    """Start a created container."""
    await run(["start", container_id], timeout=120.0)


async def wait_for_container_ready(
    container_id: str, *, timeout: float = 60.0
) -> None:
    """Block until the container's entrypoint signals readiness.

    ``podman start`` returns the moment the container reaches "running"
    state — i.e. the entrypoint has *begun*, not finished. The entrypoint
    creates ``/tmp/.klangk-ready`` once its one-time setup (on-entrypoint
    plugin hooks) is done, so this blocks until that sentinel exists and
    callers can treat the container as fully ready, not just started.

    Implemented as a single ``podman exec`` that spins on the sentinel
    file: one round-trip, no poll interval, so the only latency is the
    irreducible entrypoint work itself.

    Raises :class:`PodmanError` if the sentinel does not appear within
    *timeout* seconds.
    """
    rc, _out, _err = await exec_container(
        container_id,
        [
            "sh",
            "-c",
            "while [ ! -f /tmp/.klangk-ready ]; do sleep 0.1; done",
        ],
        timeout=timeout,
    )
    if rc != 0:
        raise PodmanError(
            500,
            f"Container {container_id} did not become ready within "
            f"{timeout}s (entrypoint did not create /tmp/.klangk-ready)",
        )


def _exec_args(
    container_id: str,
    cmd: list[str],
    *,
    user: str | None = None,
    interactive: bool = False,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Build the ``podman exec`` argument list (without the leading binary).

    All options (``-i`` / ``-u`` / ``-e``) precede the container id and
    command, matching ``podman exec [OPTIONS] CONTAINER [COMMAND...]``.
    Centralized so the three ``exec_container*`` callers stay in sync and
    there is a single place to add flags (e.g. ``extra_env``).
    """
    args = ["exec"]
    if interactive:
        args.append("-i")
    if user:
        args += ["-u", user]
    if extra_env:
        for key, value in extra_env.items():
            args += ["-e", f"{key}={value}"]
    args.append(container_id)
    args.extend(cmd)
    return args


async def exec_container(
    container_id: str,
    cmd: list[str],
    *,
    user: str | None = None,
    stdin_data: bytes | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = 30.0,
) -> tuple[int, str, str]:
    """Run a command inside a running container.

    Returns ``(returncode, stdout, stderr)``.
    """
    args = _exec_args(
        container_id,
        cmd,
        user=user,
        interactive=stdin_data is not None,
        extra_env=extra_env,
    )
    return await run(args, check=False, stdin_data=stdin_data, timeout=timeout)


async def exec_container_bytes(
    container_id: str,
    cmd: list[str],
    *,
    user: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = 30.0,
) -> tuple[int, bytes, str]:
    """Like ``exec_container`` but returns raw stdout bytes (for binary data)."""
    args = _exec_args(container_id, cmd, user=user, extra_env=extra_env)
    return await run_raw(args, check=False, timeout=timeout)


async def exec_container_stream(
    container_id: str,
    cmd: list[str],
    *,
    user: str | None = None,
    extra_env: dict[str, str] | None = None,
    chunk_size: int = 64 * 1024,
) -> AsyncGenerator[bytes, None]:
    """Stream stdout from a command inside a container.

    Uses ``stdout=PIPE`` for true end-to-end streaming without buffering
    to disk.  stderr is discarded to avoid pipe-buffer deadlocks (the
    process would block if stderr fills while we only drain stdout).
    """
    args = _exec_args(container_id, cmd, user=user, extra_env=extra_env)
    proc = await asyncio.create_subprocess_exec(
        _podman_bin(),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=subprocess_env(),
    )
    yielded = False
    try:
        while True:
            chunk = await proc.stdout.read(chunk_size)
            if not chunk:
                break
            yielded = True
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    if proc.returncode != 0:
        logger.warning(
            "exec_container_stream command failed (rc=%d): %s %s",
            proc.returncode,
            container_id,
            cmd,
        )
        # Only abort the stream if no data was produced; if data was
        # yielded the response is already in-flight and may be valid
        # (e.g. tar exits 1 when files change during archiving).
        if not yielded:
            raise PodmanError(
                proc.returncode,
                f"stream command exited with code {proc.returncode}",
            )


async def remove_container(container_id: str, *, force: bool = True) -> None:
    """Remove a container; never raises on 404."""
    args = ["rm"]
    if force:
        args.append("-f")
    args.append(container_id)
    rc, _out, err = await run(args, check=False)
    if rc != 0 and classify(err) != 404:
        raise PodmanError(classify(err), err.strip() or "podman rm")


async def list_containers(label: str) -> list[dict]:
    """List containers matching ``label`` (``key=value``)."""
    _rc, out, _err = await run(
        ["ps", "-a", "--filter", f"label={label}", "--format", "json"]
    )
    out = out.strip()
    return json.loads(out) if out else []


# --- Volumes ---


async def inspect_volume(name: str) -> dict | None:
    """Return a volume's inspect dict, or None if it does not exist."""
    rc, out, _err = await run(["volume", "inspect", name], check=False)
    if rc != 0:
        return None
    data = json.loads(out)
    return data[0] if data else None


async def create_volume(
    name: str, labels: dict[str, str] | None = None
) -> dict:
    """Create a labelled volume and return its inspect dict."""
    args = ["volume", "create"]
    for key, value in (labels or {}).items():
        args += ["--label", f"{key}={value}"]
    args.append(name)
    await run(args)
    info = await inspect_volume(name)
    if info is None:  # pragma: no cover
        raise PodmanError(500, f"volume {name!r} vanished after create")
    return info


async def list_volumes(label: str) -> list[dict]:
    """List volumes matching ``label`` (``key=value``)."""
    _rc, out, _err = await run(
        ["volume", "ls", "--filter", f"label={label}", "--format", "json"]
    )
    out = out.strip()
    return json.loads(out) if out else []


async def remove_volume(name: str) -> None:
    """Remove a volume.

    Raises :class:`PodmanError` with status 404 or 409 so callers can
    map them to HTTP responses.
    """
    rc, _out, err = await run(["volume", "rm", name], check=False)
    if rc != 0:
        raise PodmanError(classify(err), err.strip() or "podman volume rm")


# --- Exec sessions ---


class ExecSession:
    """Manages a podman exec session with raw stdin/stdout pipes (no PTY)."""

    def __init__(
        self,
        container_id: str,
        env: list[str] | None = None,
        work_dir: str = "/home/work",
    ):
        self.container_id = container_id
        self.env = env or []
        self.work_dir = work_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._output_queue: BoundedOutputQueue[bytes] = BoundedOutputQueue(
            maxsize=64
        )
        self._running = False
        self._read_task: asyncio.Task | None = None
        self._returncode: int | None = None

    async def start(self, command: list[str], *, login: bool = False) -> None:
        """Start a command via podman exec with piped stdin/stdout.

        By default *command* is passed to ``podman exec`` as raw argv
        (no shell) -- the right thing for programmatic transports like
        ``klangkc sync``'s rsync, which must NOT source startup files: a
        ``~/.profile`` that prints to stdout would corrupt the binary
        rsync stream (the classic ssh/scp footgun), and rsync's argv is
        shell-quoted precisely so a non-login round-trips cleanly.

        When *login* is set the command runs under a **login shell** that
        sources ``~/.profile`` -- matching what an interactive terminal
        sees, and what ``klangkc exec`` needs by default (#1041): a user
        typing ``klangkc exec ws openclaw --version`` expects the
        nvm-installed binary on PATH. The login shell is run as
        ``bash -lc 'exec "$@"'`` with the command as its argv -- the
        standard wrapper-script idiom that gets BOTH a login shell
        (profile sourced) AND argv fidelity (each element survives as
        one word, no quoting games). This is how ``docker exec`` /
        ``kubectl exec`` behave: the argv is exec'd, not shell-parsed,
        so a compound command needs an explicit ``bash -c`` just like
        those tools. Callers that must avoid the login shell entirely
        (rsync's binary stream) use the default ``login=False`` path.
        """
        env_flags: list[str] = []
        for entry in self.env:
            env_flags += ["-e", entry]
        if login:
            argv = ["bash", "-lc", 'exec "$@"', "bash", *command]
        else:
            argv = command
        exec_cmd = [
            _podman_bin(),
            "exec",
            "-i",
            *env_flags,
            "-u",
            "klangk",
            "-w",
            self.work_dir,
            self.container_id,
            *argv,
        ]

        self._running = True
        self._proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=subprocess_env(),
        )
        self._read_task = asyncio.create_task(self._read_stdout())
        logger.info(
            "Exec session started for container %s (login=%s): %s",
            self.container_id,
            login,
            command,
        )

    async def _read_stdout(self) -> None:
        """Read stdout in a background task and queue chunks."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                data = await self._proc.stdout.read(65536)
                if not data:
                    break
                # Bounded queue: blocks when full, back-pressuring the
                # process via its kernel pipe buffer.
                await self._output_queue.put(data)
        except asyncio.CancelledError:
            raise
        except OSError:
            pass
        # Wait for the process to exit so returncode is set before
        # the caller reads it.
        if self._proc and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (
                asyncio.TimeoutError,
                ProcessLookupError,
                OSError,
            ):  # pragma: no cover
                pass
        self._output_queue.send_sentinel()

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        if self._read_task is not None and self._read_task.done():
            return False
        return self._proc.returncode is None

    async def write(self, data: bytes) -> None:
        """Write data to the process stdin."""
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (
                BrokenPipeError,
                ConnectionResetError,
                OSError,
            ):  # pragma: no cover
                pass  # Process already exited

    async def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        if self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.close()

    async def output(self) -> AsyncGenerator[bytes, None]:
        """Yield stdout data as it arrives."""
        while self._running:
            try:
                data = await asyncio.wait_for(
                    self._output_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                # Producer finished but sentinel was dropped (queue was full).
                if self._read_task is not None and self._read_task.done():
                    break
                continue  # pragma: no cover
            if data is None:
                break
            yield data

    async def stop(self) -> None:
        """Stop the exec session and clean up."""
        self._running = False

        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover
                logger.exception("Error awaiting exec read task")
            self._read_task = None

        if self._proc:
            # Save exit code before nulling _proc so returncode stays
            # accessible after stop().
            if self._proc.returncode is not None:
                self._returncode = self._proc.returncode
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                try:
                    self._proc.kill()  # pragma: no cover
                except (ProcessLookupError, OSError):  # pragma: no cover
                    pass
            if self._proc.returncode is not None:
                self._returncode = self._proc.returncode
            self._proc = None
        logger.info("Exec session stopped for container %s", self.container_id)

    @property
    def returncode(self) -> int | None:
        """Return the process exit code, or None if still running."""
        if self._proc is not None:
            return self._proc.returncode
        return self._returncode
