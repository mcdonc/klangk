"""Persistent tmux control-mode client that reports workspace window changes.

One :class:`WindowEventWatcher` per workspace keeps every connected client's
terminal tab strip in sync with tmux. A detached ``tmux -C`` control client
emits ``%unlinked-window-add`` / ``%window-close`` / ``%session-window-changed``
events whenever a window is added, closed, or the active window switches —
and (verified empirically) these fire for any session on the tmux server, not
just the control client's own. The watcher hands each event to a callback so
the caller can debounce the burst into a single ``terminal_windows`` re-broadcast
— meaning podman execs happen only on real changes, not on a per-tick poll
(#2161 / #2171).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from ..podman import Podman, subprocess_env

logger = logging.getLogger(__name__)

# Control-mode events that mean "the window set or the active window may have
# changed". %output (pane bytes), %begin/%end (command framing), and the
# control client's own %window-add / %session-changed are deliberately ignored
# so its idle pane never trips a re-sync. %unlinked-window-renamed covers
# tmux-side window renames (#2175).
_WINDOW_EVENT_PREFIXES = (
    "%unlinked-window-add",
    "%unlinked-window-close",
    "%unlinked-window-renamed",
    "%window-close",
    "%session-window-changed",
)


def is_window_event(line: str) -> bool:
    """True if a control-mode ``line`` indicates a window/active change."""
    return line.startswith(_WINDOW_EVENT_PREFIXES)


async def _terminate_watcher_proc(proc) -> None:
    """Terminate the watcher subprocess: TERM, a 3s grace, then KILL
    (both racing a process that is already gone)."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class WindowEventWatcher:
    """A long-lived ``tmux -C`` control client for one container.

    :meth:`start` spawns it (idempotent while live or in flight);
    :meth:`stop` tears it down. A stopped watcher is single-use: a
    later :meth:`start` never spawns (#2929).
    ``on_change`` is called synchronously from the reader task once per
    relevant event; callers debounce.
    """

    def __init__(self, podman: Podman, container_id: str, on_change) -> None:
        self.podman = podman
        self._container_id = container_id
        self._on_change = on_change
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        # Start/stop race guard (#2929). ``start()``'s podman exec is a slow
        # await, and a ``stop()`` landing inside it found ``_task``/``_proc``
        # still unset and no-opped — the exec then completed into fields
        # nothing would ever tear down, orphaning the host-side reader and
        # the container-side control client. ``_starting`` marks an
        # in-flight start so re-entrant ``start()`` calls no-op; ``_stopped``
        # is set by ``stop()`` *before* it reads any field, so a racing
        # ``start()`` observes it right after its exec await and tears the
        # fresh client down itself instead of orphaning it. A stopped
        # watcher is single-use: a later ``start()`` never spawns.
        # ``_start_pending`` is True from construction (#3015 review): the
        # session spawns ``start()`` fire-and-forget, and until that task
        # first runs the watcher must already read alive — a second
        # ``add_subscriber`` in that window would otherwise discard the
        # fresh watcher and waste a control-client exec. ``start()``
        # clears it synchronously before its guards, so a direct first
        # call still spawns; only a start whose exec failed (or never
        # ran) leaves a no-task watcher reading not-alive.
        self._start_pending = True
        self._starting = False
        self._stopped = False
        # Unique per-watcher session name so a stale client from a prior
        # instance (e.g. across a container restart) never collides.
        self._ctrl_session = f"__klangk_ctrl-{uuid.uuid4().hex[:8]}"

    @property
    def container_id(self) -> str:
        """The container this watcher's control client is bound to.

        Baked in at construction, so a watcher can never be re-aimed at
        a recycled container — the session replaces it instead (#3015).
        """
        return self._container_id

    @property
    def alive(self) -> bool:
        """True while the watcher can still deliver events.

        True from construction (a start is pending — the session spawns
        it fire-and-forget, #3015 review), while the start exec is in
        flight, and while the reader task runs. False when stopped
        (single-use, #2929), when the reader task has exited — the exec
        died with its container — or when a start failed before
        spawning a reader. The session treats a watcher that is not
        alive as dead and builds a fresh one (#3015).
        """
        if self._stopped:
            return False
        if self._task is None:
            return self._starting or self._start_pending
        return not self._task.done()

    async def start(
        self,
    ) -> None:
        # Clear the pending marker synchronously before any guard: the
        # task is running now, so "constructed but never started" no
        # longer describes it (#3015 review).
        self._start_pending = False
        if self._start_in_flight_or_live():
            return
        self._starting = True
        try:
            proc = await asyncio.create_subprocess_exec(
                self.podman.bin,
                "exec",
                "-i",
                self._container_id,
                "tmux",
                "-C",
                "new-session",
                "-s",
                self._ctrl_session,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=subprocess_env(),
            )
        finally:
            self._starting = False
        if self._stopped:
            # stop() ran while the exec above was in flight: its field reads
            # saw nothing to tear down, so finish the job here rather than
            # assigning fields nothing will ever stop (#2929).
            await _terminate_watcher_proc(proc)
            await self._kill_ctrl_session()
            return
        self._proc = proc
        self._task = asyncio.create_task(self.read_loop())

    def _start_in_flight_or_live(self) -> bool:
        """True when a start would duplicate or race an existing one."""
        if self._task is not None and not self._task.done():
            return True
        return self._starting or self._stopped

    async def read_loop(self) -> None:
        stdout = self._reader_stream()
        if stdout is None:
            return
        try:
            await self._read_events(stdout)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WindowEventWatcher read loop error")

    def _reader_stream(self):
        """The proc's stdout, or None when the watcher never started."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        return proc.stdout

    async def _read_events(self, stdout) -> None:
        """Read control-mode lines forever, firing on window events."""
        while True:
            raw = await stdout.readline()
            if not raw:
                return
            if is_window_event(raw.decode(errors="replace").strip()):
                self._on_change()

    async def stop(self) -> None:
        # Set the flag before reading any teardown state, so a start()
        # whose exec is still in flight observes it once the exec returns
        # (#2929).
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        proc = self._proc
        self._proc = None
        if proc is not None:
            await _terminate_watcher_proc(proc)
            await self._kill_ctrl_session()

    async def _kill_ctrl_session(
        self,
    ) -> None:
        try:
            await self.podman.exec_container(
                self._container_id,
                ["tmux", "kill-session", "-t", self._ctrl_session],
            )
        except Exception:
            pass
