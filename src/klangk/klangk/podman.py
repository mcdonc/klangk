"""Thin async wrapper around the ``podman`` CLI.

Drives a rootless, daemonless Podman engine with no socket.  Every call
shells out via ``asyncio.create_subprocess_exec`` and parses
``--format json``.

Non-zero exits raise :class:`PodmanError` whose ``status`` mimics
HTTP-like codes (404 not-found, 409 conflict/in-use) so callers can
branch accordingly; anything else maps to 500.

The binary is configurable via the ``podman_bin`` setting (defaults to
``podman``). The :class:`Podman` class takes ``KlangkSettings`` once
(at construction, in :func:`build_app`) and carries the resolved binary
path on ``self._bin`` — no ``get_settings()`` reacharound (#1468).
Callers obtain the instance via ``app.state.podman`` (explicit threading,
the same pattern as ``container_registry`` / ``sockets``).
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncGenerator

from .util import BoundedOutputQueue

logger = logging.getLogger(__name__)

# The shared-home path inside workspace containers. Defined here — the
# lowest-level module that needs it (the ``work_dir`` default below) —
# because ``podman`` sits *below* the ``container`` package and must not
# import from it (that drags ``container/__init__`` in while this module
# is still initializing: the podman↔container.sidecar/registry import
# cycle). ``container.spec`` re-exports both so the workspaces /
# wshandler / health import sites keep working; the agent identity's
# handle is fixed to this name (#2718, immutable), one source of truth
# for the shared-layout home and the agent/service-session home (#2720).
SHARED_HOME_NAME = "klangk"
SHARED_HOME = f"/home/{SHARED_HOME_NAME}"


def subprocess_env() -> dict[str, str]:
    """Return an environment dict for podman subprocesses.

    Strips ``LD_LIBRARY_PATH`` so the podman binary uses its own
    libraries.  Nix binaries have RPATH baked in and don't need it;
    system binaries (e.g. on CI) break if nix's glibc leaks in.
    """
    return {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}


def bringup_timeout(default: float = 120.0, ci: float = 240.0) -> float:
    """Podman bring-up budget, doubled on CI (#3064).

    Four E2E suites share one CI VM; under that storage/IO contention a
    create or start can legitimately outrun the local-dev budget. Same
    load-aware shape as the frontend's container-ready doubling (#2745).
    """
    return ci if os.environ.get("CI") else default


class PodmanError(Exception):
    """A podman CLI invocation failed.

    ``status`` is an HTTP-like code (404, 409, 500) derived from stderr.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class PodmanTimeoutError(PodmanError):
    """A podman CLI invocation exceeded its timeout budget (#3064).

    Structural signal — ``_run_create``'s retry branches on this type,
    not on message text: a timed-out create that wrote partial stderr
    (image-pull progress, storage warnings) still carries the marker,
    while a non-timeout stderr that merely mentions a timeout does not
    become retryable.
    """


_NOT_FOUND_HINTS = ("no such", "not found", "no container")
_IN_USE_HINTS = ("in use", "being used", "already in use")


def _matches_any(low: str, hints: tuple[str, ...]) -> bool:
    """True when any hint appears in the lowercased stderr."""
    return any(h in low for h in hints)


def classify(stderr: str) -> int:
    """Map podman stderr text to an HTTP-like status code."""
    low = stderr.lower()
    if _matches_any(low, _NOT_FOUND_HINTS):
        return 404
    if _matches_any(low, _IN_USE_HINTS):
        return 409
    return 500


class Podman:
    """Owns the resolved podman binary path and the ~20 CLI wrappers.

    Constructed once in :func:`build_app` and stored on
    ``app.state.podman`` (#1468). The binary path is resolved once from
    ``settings.podman_bin``; methods use ``self._bin`` instead of a
    per-call ``get_settings()`` reacharound.
    """

    def __init__(self, app):
        self.app = app
        # Per-user locks serializing volume quota probe+create (#2972) —
        # the dict-of-locks pattern registry.py uses for service sessions.
        # Single event loop: no await between get and set, so no race.
        self._volume_locks: dict[str, asyncio.Lock] = {}

    def volume_create_lock(self, user_id: str) -> asyncio.Lock:
        """The lock serializing *user_id*'s quota count + volume create.

        Count-then-create must be atomic per user or N concurrent
        creates each count the same pre-create total and all pass a cap
        they jointly exceed (#2972). Both volume-create doors — the
        ``POST /volumes`` route and workspace-start auto-create of
        mounted named volumes — take this lock, so an API create and a
        workspace start cannot race each other either.
        """
        lock = self._volume_locks.get(user_id)
        if lock is None:
            lock = self._volume_locks[user_id] = asyncio.Lock()
        return lock

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def _bin(self) -> str:
        return self.app.state.settings.podman_bin

    @property
    def bin(self) -> str:
        """The resolved podman binary path (for ``ExecSession`` etc.)."""
        return self._bin

    @staticmethod
    async def _wait_podman(
        proc, timeout: float | None, cmd_label: str
    ) -> bool:
        """Wait for a podman process, killing it on timeout; returns whether
        it timed out."""
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return False
        except asyncio.TimeoutError:
            logger.warning(
                "podman-timeout: %s exceeded %.1fs — killing",
                cmd_label,
                timeout,
            )
            proc.kill()
            await proc.wait()
            return True

    @staticmethod
    def _timeout_rc(
        err: str,
        cmd_label: str,
        timeout: float | None,
        timed_out: bool,
        rc: int,
    ) -> tuple[int, str]:
        """(rc, err) with the timeout override applied."""
        if not timed_out:
            return rc, err
        return -1, err or f"{cmd_label} timed out after {timeout}s"

    @staticmethod
    def _log_podman_timing(cmd_label: str, timings, err: str) -> None:
        """Debug-log the phase timings; a slow run's stderr rides along."""
        t0, t1, t2, t3 = timings
        elapsed = t3 - t0
        logger.debug(
            "podman-timing: %s tempfile=%.3fs spawn=%.3fs wait=%.3fs"
            " total=%.3fs",
            cmd_label,
            t1 - t0,
            t2 - t1,
            t3 - t2,
            elapsed,
        )
        if elapsed > 2.0 and err.strip():
            logger.debug(
                "podman-timing: %s stderr: %s", cmd_label, err.strip()
            )

    @staticmethod
    def _finish_podman_run(
        proc,
        timed_out: bool,
        err: str,
        cmd_label: str,
        timeout: float | None,
        args: list[str],
        check: bool,
        timings: tuple[float, float, float, float],
    ) -> tuple[int, str]:
        """Apply the timeout override, log timings, and enforce *check*."""
        rc = proc.returncode or 0
        rc, err = Podman._timeout_rc(err, cmd_label, timeout, timed_out, rc)
        Podman._log_podman_timing(cmd_label, timings, err)
        if check and rc != 0:
            Podman._raise_podman_error(err, args, timed_out)
        return rc, err

    @staticmethod
    def _raise_podman_error(
        err: str, args: list[str], timed_out: bool
    ) -> None:
        """Raise the right error for a failed checked run: the structural
        PodmanTimeoutError marker on a budget overrun (#3064), else the
        plain classified PodmanError."""
        error_class = PodmanTimeoutError if timed_out else PodmanError
        raise error_class(classify(err), err.strip() or f"podman {args[0]}")

    async def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        stdin_data: bytes | None = None,
        timeout: float | None = 30.0,
    ) -> tuple[int, str, str]:
        """Run ``podman <args>`` and return ``(returncode, stdout, stderr)``
        (stdout decoded as UTF-8 text).

        See :meth:`run_raw` — this is the text-decoding wrapper over it.
        """
        rc, out_bytes, err = await self.run_raw(
            args, check=check, stdin_data=stdin_data, timeout=timeout
        )
        return rc, out_bytes.decode("utf-8", errors="replace"), err

    async def run_raw(
        self,
        args: list[str],
        *,
        check: bool = True,
        stdin_data: bytes | None = None,
        timeout: float | None = 30.0,
    ) -> tuple[int, bytes, str]:
        """Like ``run`` but returns raw stdout bytes (for binary data).

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
                self._bin,
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
            timed_out = await self._wait_podman(proc, timeout, cmd_label)
            t3 = time.monotonic()
            out_f.seek(0)
            err_f.seek(0)
            out_bytes = out_f.read()
            err = err_f.read().decode("utf-8", errors="replace")
        rc, err = self._finish_podman_run(
            proc,
            timed_out,
            err,
            cmd_label,
            timeout,
            args,
            check,
            (t0, t1, t2, t3),
        )
        return rc, out_bytes, err

    # --- Containers ---

    async def _inspect_first(self, args: list[str]) -> dict | None:
        """Run a ``podman inspect``-style command; first record or None."""
        rc, out, _err = await self.run(args, check=False)
        if rc != 0:
            return None
        data = json.loads(out)
        return data[0] if data else None

    async def _list_json(self, args: list[str]) -> list[dict]:
        """Run a listing command; parsed JSON output (empty list if none)."""
        _rc, out, _err = await self.run(args)
        out = out.strip()
        return json.loads(out) if out else []

    async def inspect_container(self, container_id: str) -> dict | None:
        """Return the inspect dict for a container, or None if it is gone."""
        return await self._inspect_first(
            ["container", "inspect", container_id]
        )

    async def container_logs(self, container_id: str) -> str:
        """Return the container's combined stdout/stderr logs (empty if gone).

        Used to wait for a container's entrypoint to signal readiness (e.g. the
        network sidecar's proxy prints ``dns-proxy listening`` once bound) —
        ``podman start`` returns the moment the container reaches "running",
        which is before the entrypoint has finished its setup (#2277).
        """
        rc, out, _err = await self.run(["logs", container_id], check=False)
        return out if rc == 0 else ""

    @staticmethod
    def _create_base_args(
        name: str,
        pull: str,
        replace: bool,
        init: bool,
        interactive: bool,
        userns: str | None,
    ) -> list[str]:
        """``create`` subcommand + name/pull/mode/userns flags."""
        args = ["create", f"--pull={pull}", "--name", name]
        if replace:
            args.append("--replace")
        if init:
            args.append("--init")
        if interactive:
            args.append("-i")
        if userns:
            args += ["--userns", userns]
        return args

    @staticmethod
    def _create_capability_args(
        cap_drop: list[str] | None, cap_add: list[str] | None
    ) -> list[str]:
        """One ``--cap-drop`` / ``--cap-add`` flag per entry."""
        args: list[str] = []
        for cap in cap_drop or []:
            args += ["--cap-drop", cap]
        for cap in cap_add or []:
            args += ["--cap-add", cap]
        return args

    @staticmethod
    def _create_resource_args(
        cpus: float | None, memory: str | None, pids_limit: int | None
    ) -> list[str]:
        """Deploy-wide resource caps (#34) — each flag only when non-None,
        so an unset limit = no flag = no behavior change."""
        args: list[str] = []
        if cpus is not None:
            args += ["--cpus", str(cpus)]
        if memory is not None:
            args += ["--memory", memory]
        if pids_limit is not None:
            args += ["--pids-limit", str(pids_limit)]
        return args

    @staticmethod
    def _create_label_args(
        labels: dict[str, str] | None, annotations: dict[str, str] | None
    ) -> list[str]:
        """``--label`` / ``--annotation`` flags (per-workspace OCI-hook
        annotations, #1770)."""
        args: list[str] = []
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        for key, value in (annotations or {}).items():
            args += ["--annotation", f"{key}={value}"]
        return args

    @staticmethod
    def _hooks_dir_args(hooks_dir: list[str] | None) -> list[str]:
        """``--hooks-dir`` global flags — podman global flags precede the
        subcommand, and podman does not persist them from create to
        start."""
        args: list[str] = []
        for d in hooks_dir or []:
            args += ["--hooks-dir", d]
        return args

    @staticmethod
    def _publish_args(publish: list | None) -> list[str]:
        """``-p`` flags for ``(host_port, container_port)`` or
        ``(bind_addr, host_port, container_port)`` entries."""
        args: list[str] = []
        for entry in publish or []:
            if len(entry) == 3:
                bind, host_port, container_port = entry
                args += ["-p", f"{bind}:{host_port}:{container_port}"]
            else:
                host_port, container_port = entry
                args += ["-p", f"{host_port}:{container_port}"]
        return args

    @staticmethod
    def _create_storage_args(
        binds: list[str] | None,
        tmpfs: dict[str, str] | None,
        publish: list[tuple[int, int] | tuple[str, int, int]] | None,
    ) -> list[str]:
        """Volume / tmpfs / port-publish flags (``publish`` entries are
        ``(host_port, container_port)`` or ``(bind_addr, host_port,
        container_port)``)."""
        args: list[str] = []
        for bind in binds or []:
            args += ["-v", bind]
        for path, opts in (tmpfs or {}).items():
            args += ["--tmpfs", f"{path}:{opts}"]
        return args + Podman._publish_args(publish)

    @staticmethod
    def _create_dns_args(
        add_hosts: list[str] | None,
        dns: list[str] | None,
        dns_search: list[str] | None,
    ) -> list[str]:
        """Hosts-file / DNS server / DNS search flags."""
        args: list[str] = []
        for flag, values in (
            ("--add-host", add_hosts),
            ("--dns", dns),
            ("--dns-search", dns_search),
        ):
            for value in values or []:
                args += [flag, value]
        return args

    async def create_container(
        self,
        name: str,
        image: str,
        *,
        labels: dict[str, str] | None = None,
        binds: list[str] | None = None,
        tmpfs: dict[str, str] | None = None,
        publish: list[tuple[int, int] | tuple[str, int, int]] | None = None,
        add_hosts: list[str] | None = None,
        dns: list[str] | None = None,
        dns_search: list[str] | None = None,
        env: list[str] | None = None,
        annotations: dict[str, str] | None = None,
        hooks_dir: list[str] | None = None,
        init: bool = False,
        interactive: bool = False,
        pull: str = "never",
        replace: bool = True,
        userns: str | None = None,
        cap_drop: list[str] | None = None,
        cap_add: list[str] | None = None,
        cpus: float | None = None,
        memory: str | None = None,
        pids_limit: int | None = None,
        command: list[str] | None = None,
        network: str | None = None,
    ) -> str:
        """Create a container and return its id.

        ``publish`` is a list of ``(host_port, container_port)`` or
        ``(bind_addr, host_port, container_port)`` tuples.
        ``replace=True`` removes an existing container with the same name.
        ``command`` (optional ``list[str]``) overrides the image ``Cmd``:
        the args are appended after the image name (e.g. LiteLLM's
        ``--config /app/config.yaml``).
        ``annotations``/``hooks_dir`` carry per-workspace OCI hooks: each
        annotation becomes a ``--annotation key=value`` flag, and each
        ``hooks_dir`` entry becomes a ``--hooks-dir`` flag. ``--hooks-dir``
        overrides (does not append) podman's default hook search paths, so
        a caller passing its own dir repeats the standard default dirs to
        keep operator createContainer hooks running (#1770); callers that
        set no hooks omit the flag entirely (no behavior change). (The
        egress filter moved to the network sidecar and no longer uses
        these, #2255; they remain for general OCI-hook consumers.) ``cap_drop``
        becomes one ``--cap-drop`` flag each (the workspace container
        always drops ``net_raw``, #2347). ``cap_add`` becomes one
        ``--cap-add`` flag each (the network sidecar uses it for
        ``NET_ADMIN``/``NET_RAW`` — RST forging and the egress ruleset,
        #2345; workspaces never pass it, #2347). ``cpus``/``memory``/
        ``pids_limit`` are the
        deploy-wide resource caps (#34): each emits its flag **only when
        non-None**, so an unset limit = no flag = no behavior change — the
        same omit-when-unset posture as ``cap_drop``/``userns``.
        """
        # --hooks-dir is a podman global flag (before the subcommand), not a
        # create flag. Placing it after "create" causes podman to silently
        # ignore it. Global flags must precede the subcommand.
        args: list[str] = self._hooks_dir_args(hooks_dir)
        args += self._create_base_args(
            name, pull, replace, init, interactive, userns
        )
        args += self._create_capability_args(cap_drop, cap_add)
        args += self._create_resource_args(cpus, memory, pids_limit)
        if network:
            args += ["--network", network]
        args += self._create_label_args(labels, annotations)
        args += self._create_storage_args(binds, tmpfs, publish)
        args += self._create_dns_args(add_hosts, dns, dns_search)
        for entry in env or []:
            args += ["-e", entry]
        args.append(image)
        args += command or []
        _rc, out, _err = await self._run_create(args, replace)
        return out.strip()

    async def _run_create(
        self, args: list[str], replace: bool
    ) -> tuple[int, str, str]:
        """Run ``podman create`` with one timeout retry (#3064).

        A create that stalls past its budget under concurrent load used to
        cascade into every downstream waiter (workspace start, WS connect,
        teardown). With ``replace=True`` (``create_container``'s default)
        a retried create is idempotent — the stalled attempt was killed,
        run it once more before giving up. Without it there is no safe
        retry: the first, killed attempt may have left the name claimed,
        so the timeout surfaces immediately instead.
        """
        try:
            return await self.run(args, timeout=bringup_timeout())
        except PodmanTimeoutError:
            if not replace:
                raise
        logger.warning("podman create stalled past its budget; retrying once")
        return await self.run(args, timeout=bringup_timeout())

    async def start_container(
        self,
        container_id: str,
        hooks_dir: list[str] | None = None,
    ) -> None:
        """Start a created container.

        ``hooks_dir`` is the same list passed to ``create_container``:
        ``--hooks-dir`` is a podman **global** flag that must be present on
        the ``start`` invocation too — podman does not persist it from
        ``create``.  OCI hooks are discovered and executed at ``start``
        time, so omitting the flag here silently skips all hooks.
        """
        args: list[str] = self._hooks_dir_args(hooks_dir)
        args += ["start", container_id]
        # Same CI contention family as create (#3064): budget only, no
        # retry — start is not idempotent the way --replace create is.
        await self.run(args, timeout=bringup_timeout())

    async def wait_for_container_ready(
        self, container_id: str, *, timeout: float = 60.0
    ) -> None:
        """Block until the container's entrypoint signals readiness.

        ``podman start`` returns the moment the container reaches "running"
        state — i.e. the entrypoint has *begun*, not finished. The entrypoint
        creates ``/tmp/.klangk-ready`` once its one-time setup (on-entrypoint
        feature hooks) is done, so this blocks until that sentinel exists and
        callers can treat the container as fully ready, not just started.

        Implemented as a single ``podman exec`` that spins on the sentinel
        file: one round-trip, no poll interval, so the only latency is the
        irreducible entrypoint work itself.

        Raises :class:`PodmanError` if the sentinel does not appear within
        *timeout* seconds.
        """
        rc, _out, _err = await self.exec_container(
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

    @staticmethod
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
        self,
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
        args = self._exec_args(
            container_id,
            cmd,
            user=user,
            interactive=stdin_data is not None,
            extra_env=extra_env,
        )
        return await self.run(
            args, check=False, stdin_data=stdin_data, timeout=timeout
        )

    async def exec_container_bytes(
        self,
        container_id: str,
        cmd: list[str],
        *,
        user: str | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: float | None = 30.0,
    ) -> tuple[int, bytes, str]:
        """Like ``exec_container`` but returns raw stdout bytes."""
        args = self._exec_args(
            container_id, cmd, user=user, extra_env=extra_env
        )
        return await self.run_raw(args, check=False, timeout=timeout)

    def _stream_failure(
        self, proc, container_id: str, cmd: list[str], yielded: bool
    ) -> None:
        """Warn on a failed stream; abort (raise) only when no data was
        produced — if data was yielded the response is already in-flight
        and may be valid (e.g. tar exits 1 when files change during
        archiving)."""
        if proc.returncode == 0:
            return
        logger.warning(
            "exec_container_stream command failed (rc=%d): %s %s",
            proc.returncode,
            container_id,
            cmd,
        )
        if not yielded:
            raise PodmanError(
                proc.returncode,
                f"stream command exited with code {proc.returncode}",
            )

    async def exec_container_stream(
        self,
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
        args = self._exec_args(
            container_id, cmd, user=user, extra_env=extra_env
        )
        proc = await asyncio.create_subprocess_exec(
            self._bin,
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
        self._stream_failure(proc, container_id, cmd, yielded)

    async def _stop_before_remove(self, container_id: str) -> bool:
        """Graceful stop first (conmon cleanup kills pasta). False when
        the container is already gone — remove is then a no-op."""
        rc, _out, err = await self.run(
            ["stop", "-t", "5", container_id], check=False
        )
        return not (rc != 0 and classify(err) == 404)

    def _raise_rm_error(self, rc: int, err: str) -> None:
        """Raise for a failed ``podman rm``, except when the container is
        already gone."""
        if rc != 0 and classify(err) != 404:
            raise PodmanError(classify(err), err.strip() or "podman rm")

    async def remove_container(
        self, container_id: str, *, force: bool = True
    ) -> None:
        """Stop (if running) and remove a container; never raises on 404.

        Uses ``podman stop`` before ``podman rm`` so the full cleanup path
        runs — including pasta/passt network teardown.  A bare
        ``podman rm -f`` skips cleanup and can leave orphaned pasta
        processes holding ports indefinitely (podman#14276).
        """
        if force and not await self._stop_before_remove(container_id):
            return  # already gone
        args = ["rm"]
        if force:
            args.append("-f")  # catch stragglers
        args.append(container_id)
        rc, _out, err = await self.run(args, check=False)
        self._raise_rm_error(rc, err)

    async def list_containers(self, label: str) -> list[dict]:
        """List containers matching ``label`` (``key=value``)."""
        return await self._list_json(
            ["ps", "-a", "--filter", f"label={label}", "--format", "json"]
        )

    # --- Volumes ---

    async def inspect_volume(self, name: str) -> dict | None:
        """Return a volume's inspect dict, or None if it does not exist."""
        return await self._inspect_first(["volume", "inspect", name])

    async def create_volume(
        self, name: str, labels: dict[str, str] | None = None
    ) -> dict:
        """Create a labelled volume and return its inspect dict."""
        args = ["volume", "create"]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        args.append(name)
        await self.run(args)
        info = await self.inspect_volume(name)
        if info is None:
            raise PodmanError(500, f"volume {name!r} vanished after create")
        return info

    async def list_volumes(self, label: str) -> list[dict]:
        """List volumes matching ``label`` (``key=value``)."""
        return await self._list_json(
            ["volume", "ls", "--filter", f"label={label}", "--format", "json"]
        )

    async def count_user_volumes(self, instance: str, user_id: str) -> int:
        """Count *user_id*'s instance-managed volumes (#2972 quota).

        The same label rule ``GET /volumes`` uses to build the user's
        list: instance-label-filtered ``volume ls`` (podman filters),
        then the ``klangk.user-id`` label match in Python. The instance
        label is re-checked defensively — a stray out-of-band volume
        must not consume quota — so the count can only ever be a
        subset of what GET returns, never more.
        """
        volumes = await self.list_volumes(f"klangk.instance={instance}")
        return sum(1 for v in volumes if _is_user_volume(v, instance, user_id))

    async def remove_volume(self, name: str) -> None:
        """Remove a volume.

        Raises :class:`PodmanError` with status 404 or 409 so callers can
        map them to HTTP responses.
        """
        rc, _out, err = await self.run(["volume", "rm", name], check=False)
        if rc != 0:
            raise PodmanError(classify(err), err.strip() or "podman volume rm")


# --- Exec sessions ---


def _is_user_volume(v: dict, instance: str, user_id: str) -> bool:
    """The quota-matching label rule: the instance label is re-checked
    defensively — a stray out-of-band volume must not consume quota —
    plus the user-id label (#2972)."""
    labels = v.get("Labels") or {}
    return (
        labels.get("klangk.instance") == instance
        and labels.get("klangk.user-id") == user_id
    )


class ExecSession:
    """Manages a podman exec session with raw stdin/stdout pipes (no PTY)."""

    def __init__(
        self,
        container_id: str,
        podman: Podman,
        env: list[str] | None = None,
        work_dir: str = SHARED_HOME,
    ):
        self.container_id = container_id
        self.podman = podman
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
        ``klangk sync``'s rsync, which must NOT source startup files: a
        ``~/.profile`` that prints to stdout would corrupt the binary
        rsync stream (the classic ssh/scp footgun), and rsync's argv is
        shell-quoted precisely so a non-login round-trips cleanly.

        When *login* is set the command runs under a **login shell** that
        sources ``~/.profile`` -- matching what an interactive terminal
        sees, and what ``klangk exec`` needs by default (#1041): a user
        typing ``klangk exec ws openclaw --version`` expects the
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
            self.podman.bin,
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

    async def _pump_stdout(self, stdout) -> None:
        """Read chunks to the bounded queue (a full queue blocks,
        back-pressuring the process via its kernel pipe buffer)."""
        while True:
            data = await stdout.read(65536)
            if not data:
                break
            await self._output_queue.put(data)

    async def _await_proc_exit(self) -> None:
        """Wait (bounded) for the process to exit so returncode is set
        before the caller reads it."""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (
            asyncio.TimeoutError,
            ProcessLookupError,
            OSError,
        ):
            pass

    async def _read_stdout(self) -> None:
        """Read stdout in a background task and queue chunks."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            await self._pump_stdout(self._proc.stdout)
        except asyncio.CancelledError:
            raise
        except OSError:
            pass
        await self._await_proc_exit()
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
            ):
                pass  # Process already exited

    async def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        if self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.close()

    def _read_task_done(self) -> bool:
        """True when the stdout producer finished (its sentinel may have
        been dropped when the queue was full)."""
        return self._read_task is not None and self._read_task.done()

    async def output(self) -> AsyncGenerator[bytes, None]:
        """Yield stdout data as it arrives."""
        while self._running:
            try:
                data = await asyncio.wait_for(
                    self._output_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if self._read_task_done():
                    break
                continue
            if data is None:
                break
            yield data

    async def _cancel_read_task(self) -> None:
        """Cancel (and await) the stdout read task."""
        if self._read_task is None:
            return
        self._read_task.cancel()
        try:
            await self._read_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error awaiting exec read task")
        self._read_task = None

    def _save_returncode(self) -> None:
        """Record the current exit code (when set) for post-stop reads."""
        if self._proc is not None and self._proc.returncode is not None:
            self._returncode = self._proc.returncode

    async def _terminate_proc(self) -> None:
        """Terminate the exec process — TERM, 5s wait, KILL — saving its
        exit code so ``returncode`` stays accessible after stop()."""
        if not self._proc:
            return
        self._save_returncode()
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError, OSError):
            try:
                self._proc.kill()
            except (ProcessLookupError, OSError):
                pass
        self._save_returncode()
        self._proc = None

    async def stop(self) -> None:
        """Stop the exec session and clean up."""
        self._running = False
        await self._cancel_read_task()
        await self._terminate_proc()
        logger.info("Exec session stopped for container %s", self.container_id)

    @property
    def returncode(self) -> int | None:
        """Return the process exit code, or None if still running."""
        if self._proc is not None:
            return self._proc.returncode
        return self._returncode
