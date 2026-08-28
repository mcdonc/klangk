"""Process-launch ledger: host-side ``/proc`` capture with attribution (#2520).

Records every process launched inside a running workspace container —
launch event, argv, timestamp, and a best-effort **principal**
attribution (``agent``, ``user:<handle>``, ``unknown``). The goal is an
audit data point ("was this launched by the agent or manually by the
user"), not real-time policing or gating.

Architecture (performance contract: poll interval ≤ 80 ms at ≤ 1% of one
core at ~12k processes — mutually exclusive in Python, so):

- A small **C watcher subprocess** (``procleddy``) turns ``/proc`` into an
  ordered NDJSON event stream on stdout: ``birth`` (pid, ppid captured at
  first sight, uid, comm, argv), ``exec``, ``exit``, ``reparent``,
  ``euid_change``, ``heartbeat``. Scope (workspace root pids) is pushed to
  it on stdin. Crash-isolated — klangkd restarts it and marks a coverage
  gap; a watcher segfault must never take the server down.
- :class:`ProcessLedger` (this module, Python) owns everything the watcher
  must not know: workspace identity, attribution anchors, storage,
  retention. It joins events to workspaces by ppid-walking to root pids
  (``podman inspect .State.Pid`` of running containers) and attributes
  launches by descent from anchor panes (agent service windows, user
  terminal windows).
- **Fallback:** when the helper binary is missing or exits unrecoverably,
  a pure-Python poller takes over at a budget-derived multi-second
  interval with the effective interval surfaced loudly — the ledger never
  silently claims coverage it can't deliver.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Attribution methods (the ``attribution_method`` column): how the
# principal on a row was derived. ``anchor`` = ppid-walk to a known
# anchor pane; ``fallback`` = Python poller caught the launch but no
# anchor was resolvable (degraded mode); ``unknown`` = no attribution
# possible.
ATTR_ANCHOR = "anchor"
ATTR_FALLBACK = "fallback"

# Event types the C watcher emits (see scripts/procleddy/procleddy.c).
EVT_BIRTH = "birth"
EVT_EXEC = "exec"
EVT_EXIT = "exit"
EVT_REPARENT = "reparent"
EVT_EUID = "euid_change"
EVT_HEARTBEAT = "heartbeat"
EVT_SNAPSHOT_START = "snapshot_start"
EVT_SNAPSHOT_END = "snapshot_end"

# How long a workspace root pid may stay unresponsive to scope refresh
# before its subtree is pruned from the event-join maps.
_ROOT_TTL_S = 30.0

# Grace between watcher restarts before the ledger gives up on the C
# helper and falls back to the Python poller.
_WATCHER_MAX_RESTARTS = 3
_WATCHER_RESTART_WINDOW_S = 60.0


def _read_file(path: str) -> str | None:
    """Best-effort small-file read (``None`` on any failure/race)."""
    try:
        with open(path, "rb") as f:
            return f.read(4096).decode("utf-8", "replace")
    except OSError:
        return None


def read_comm(root: str, pid: int) -> str | None:
    s = _read_file(os.path.join(root, str(pid), "comm"))
    return s.strip("\n") if s else None


def read_argv(root: str, pid: int) -> str | None:
    s = _read_file(os.path.join(root, str(pid), "cmdline"))
    if s is None:
        return None
    return " ".join(s.split("\0")).strip()


def read_ppid_uid(root: str, pid: int) -> tuple[int, int] | None:
    """Parse PPid + real Uid from ``status``; None on failure/race."""
    s = _read_file(os.path.join(root, str(pid), "status"))
    if s is None:
        return None
    ppid = uid = None
    for line in s.splitlines():
        if line.startswith("PPid:"):
            try:
                ppid = int(line.split()[1])
            except (IndexError, ValueError):
                return None
        elif line.startswith("Uid:"):
            try:
                uid = int(line.split()[1])
            except (IndexError, ValueError):
                return None
        if ppid is not None and uid is not None:
            return ppid, uid
    return None


class _ProcSnapshot:
    """One scan of a (fake or real) ``/proc`` tree: pid -> (ppid, uid).

    Shared by the fallback poller and the anchor resolver; both must
    tolerate ENOENT races (a pid vanishing mid-scan is skipped, not an
    error).
    """

    def __init__(self, root: str = "/proc") -> None:
        self.root = root
        self.entries: dict[int, tuple[int, int]] = {}

    def scan(self) -> "_ProcSnapshot":
        try:
            names = os.listdir(self.root)
        except OSError:
            return self
        entries: dict[int, tuple[int, int]] = {}
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            pu = read_ppid_uid(self.root, pid)
            if pu is not None:
                entries[pid] = pu
        self.entries = entries
        return self


class ProcessLedger:
    """Owned subsystem: capture launches + attribute principals (#2520).

    Constructed once at startup (``app.state.process_ledger``). Takes only
    ``app`` and caches only ``self.app`` (#1608 style) — all
    settings-derived values are read live via properties so a SIGHUP swap
    propagates without a bespoke ``reconfigure``.
    """

    def __init__(self, app):
        self.app = app
        # workspace_id -> root pid (container init's host pid)
        self._roots: dict[str, int] = {}
        self._roots_by_pid: dict[int, str] = {}
        self._roots_at: dict[str, float] = {}
        # Anchor pids: pane shells whose launches get attributed.
        # pid -> ("agent", workspace_id) | ("user:<handle>", workspace_id)
        self._anchors: dict[int, tuple[str, str]] = {}
        # Container-internal anchor pids pending translation (pane pids
        # reported by tmux inside the workspace).
        self._canchors: dict[int, tuple[str, str]] = {}
        self._canchors_dirty: set[str] = set()
        # Last-input hint per workspace: workspace_id -> (handle, ts)
        # (who last typed into any of the workspace's panes).
        self._last_input: dict[str, tuple[str, float]] = {}
        # Fallback-poller previous snapshot (pid set) for diff detection.
        self._prev_seen: set[int] | None = None
        self._task: asyncio.Task | None = None
        self._watcher_proc: asyncio.subprocess.Process | None = None
        self._watcher_reader: asyncio.Task | None = None
        self._watcher_restarts: list[float] = []
        # Coverage bookkeeping surfaced via status(): how many events the
        # active backend captured, and the effective poll interval.
        self.effective_interval_ms: float | None = None
        self.backend: str = "stopped"
        self.started_at: float | None = None
        self.gaps: list[tuple[float, float]] = []  # (from, to) coverage gaps

    # ------------------------------------------------------- settings
    @property
    def enabled(self) -> bool:
        return bool(self.app.state.settings.process_ledger_enabled)

    @property
    def interval_target_s(self) -> float:
        v = self.app.state.settings.process_ledger_interval_ms
        return max(0.005, float(v) / 1000.0)

    @property
    def fallback_interval_s(self) -> float:
        """Budget-derived Python-poller interval (degraded mode)."""
        return float(
            self.app.state.settings.process_ledger_fallback_interval_s
        )

    @property
    def watcher_path(self) -> Path:
        """Watcher binary for the configured backend (#2520).

        An explicit ``process_ledger_watcher`` path overrides either
        backend's default; otherwise the backend picks its wheel-adjacent
        binary (procleddy for the /proc poller, procleddy-ebpf for the
        eBPF monitor).
        """
        explicit = self.app.state.settings.process_ledger_watcher
        if explicit:
            return Path(explicit)
        name = (
            "procleddy-ebpf"
            if self.app.state.settings.process_ledger_backend == "ebpf"
            else "procleddy"
        )
        return Path(__file__).parent / name

    # ------------------------------------------------------- anchors
    def set_root(self, workspace_id: str, pid: int) -> None:
        """Record/refresh a workspace's container-init host pid."""
        old = self._roots.get(workspace_id)
        if old is not None and old != pid:
            self._roots_by_pid.pop(old, None)
        self._roots[workspace_id] = pid
        self._roots_by_pid[pid] = workspace_id
        self._roots_at[workspace_id] = time.time()

    def drop_root(self, workspace_id: str) -> None:
        old = self._roots.pop(workspace_id, None)
        if old is not None:
            self._roots_by_pid.pop(old, None)
        self._roots_at.pop(workspace_id, None)
        self._anchors = {
            p: a for p, a in self._anchors.items() if a[1] != workspace_id
        }
        self._canchors = {
            p: a for p, a in self._canchors.items() if a[1] != workspace_id
        }
        self._canchors_dirty.discard(workspace_id)
        self._last_input.pop(workspace_id, None)

    def set_anchor(self, pid: int, principal: str, workspace_id: str) -> None:
        """Record an anchor pane pid (agent service window / user window).

        *pid* is a container-internal pid (from ``#{pane_pid}`` inside the
        workspace). It is stored as-is; ``resolve_anchors`` translates the
        container pids of each workspace to host pids (via NSpid) once per
        refresh, and the translated map is what attribution consults.
        """
        if pid <= 0:
            return
        self._canchors[pid] = (principal, workspace_id)
        self._canchors_dirty.add(workspace_id)

    def resolve_anchors(self) -> None:
        """Translate container-pid anchors to host pids (NSpid walk).

        For each workspace with pending anchors, BFS from the workspace's
        root pid to find subtree members (O(subtree) instead of
        O(all-pids × walk-depth)). A process whose ``status`` NSpid tail
        equals a stored container pid gets a host-pid anchor. Runs from the
        refresh loop (5 s) — anchor registration is eventual, which the
        80 ms watcher's ancestry chain absorbs (the anchor's own shell is
        typically long-lived).
        """
        if not self._canchors_dirty:
            return
        snap = _ProcSnapshot().scan()
        # Build parent -> children index once for all dirty workspaces.
        children: dict[int, list[int]] = {}
        for pid, (ppid, _uid) in snap.entries.items():
            children.setdefault(ppid, []).append(pid)
        for ws_id in list(self._canchors_dirty):
            root = self._roots.get(ws_id)
            if root is None:
                self._canchors_dirty.discard(ws_id)
                continue
            # BFS from root to collect subtree members.
            members: list[int] = []
            queue = [root]
            while queue:
                cur = queue.pop()
                members.append(cur)
                queue.extend(children.get(cur, ()))
            # nspid tail -> host pid for members
            for hpid in members:
                tail = _read_nspid_tail(hpid, snap.root)
                if tail is None:
                    continue
                rec = self._canchors.get(tail)
                if rec and rec[1] == ws_id:
                    self._anchors[hpid] = rec
            self._canchors_dirty.discard(ws_id)

    def prune_stale_anchors(self) -> None:
        """Drop translated anchors whose host pid no longer exists."""
        snap = _ProcSnapshot().scan()
        self._anchors = {
            p: a for p, a in self._anchors.items() if p in snap.entries
        }

    def note_input(self, workspace_id: str, handle: str) -> None:
        """Record who last typed into one of the workspace's panes."""
        self._last_input[workspace_id] = (handle, time.time())

    def prune_stale_roots(self) -> None:
        """Drop roots not refreshed within the TTL (container gone)."""
        cutoff = time.time() - _ROOT_TTL_S
        for ws in [w for w, t in self._roots_at.items() if t < cutoff]:
            self.drop_root(ws)

    # ------------------------------------------------- attribution
    def attribute(
        self, pid: int, ppid_at_birth: int, workspace_id: str, ts: float
    ) -> tuple[str, str | None]:
        """Resolve (principal, attribution_method) for a launch.

        Walks ppid edges at event time. The direct-parent check covers the
        common case (launch from a pane shell); deeper walks would need the
        full snapshot, which the C path doesn't carry into Python — for
        those, ancestry resolves via the anchor pid itself being an
        ancestor (checked against the watcher's cached ppid chain, which
        arrives with the event in ``ancestry``).
        """
        anchor = self._anchors.get(ppid_at_birth)
        if anchor is not None and anchor[1] == workspace_id:
            return anchor[0], ATTR_ANCHOR
        # The event may carry the watcher-resolved ancestry chain.
        return "", None

    def attribute_with_ancestry(
        self,
        ancestry: list[int],
        workspace_id: str,
    ) -> tuple[str, str | None]:
        """Attribute via a pid chain (deepest anchor wins)."""
        for pid in ancestry:
            anchor = self._anchors.get(pid)
            if anchor is not None and anchor[1] == workspace_id:
                return anchor[0], ATTR_ANCHOR
        return "", None

    def input_hint(self, workspace_id: str, ts: float) -> str | None:
        """Last-input hint column, if fresh (≤ 30 s before the launch)."""
        rec = self._last_input.get(workspace_id)
        if rec is None:
            return None
        handle, at = rec
        if ts - at > 30.0:
            return None
        return handle

    def workspace_for_pid(self, pid: int, entries: dict[int, tuple[int, int]]):
        """Resolve the workspace a pid belongs to, via ppid-walk.

        Returns ``(workspace_id, chain)`` — the chain is the walked
        ancestor pids (deepest last), used for anchor matching. ``None``
        when the walk leaves the map without hitting a root (host process
        or raced exit).
        """
        chain: list[int] = []
        cur: int | None = pid
        seen: set[int] = set()
        while cur is not None and cur not in seen and cur > 1:
            seen.add(cur)
            ws = self._roots_by_pid.get(cur)
            if ws is not None:
                # chain includes the root: anchors often ARE the root or
                # its pane-shell children (matching the watcher).
                chain.append(cur)
                return ws, chain
            chain.append(cur)
            cur = entries.get(cur, (None, None))[0]
        return None, chain

    # -------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Start capture (no-op when disabled by settings)."""
        if not self.enabled or self._task is not None:
            return
        self.started_at = time.time()
        if await self._start_watcher():
            ebpf = (
                self.app.state.settings.process_ledger_backend == "ebpf"
            )
            self.backend = "ebpf" if ebpf else "c-watcher"
            if ebpf:
                logger.info(
                    "process-ledger: capture running — eBPF watcher pid %s "
                    "(event-driven, no polling)",
                    self._watcher_proc.pid,
                )
            else:
                logger.info(
                    "process-ledger: capture running — C watcher pid %s, "
                    "target interval %.0f ms",
                    self._watcher_proc.pid,
                    self.interval_target_s * 1000.0,
                )
        else:
            self.backend = "python-fallback"
            self.effective_interval_ms = self.fallback_interval_s * 1000.0
            logger.info(
                "process-ledger: capture running — Python poller, "
                "effective interval ~%.1fs (degraded)",
                self.fallback_interval_s,
            )
        self._task = asyncio.create_task(self._run(), name="process-ledger")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._stop_watcher()

    def reconfigure(self, app) -> None:  # noqa: ARG002
        """SIGHUP swap: nothing cached beyond ``self.app`` (read live)."""
        # Enable/disable transitions are picked up by _run()'s settings
        # check each cycle; a disable stops the loop on its next tick.
        if not self.enabled and self._task is not None:
            t = asyncio.create_task(self.stop(), name="process-ledger-stop")
            t.add_done_callback(_log_task_exception)

    # -------------------------------------------------- C watcher
    async def _start_watcher(self) -> bool:
        """Spawn the C helper; False (and logged) if unavailable."""
        path = self.watcher_path
        if not path.exists():
            logger.warning(
                "process-ledger: C watcher not found at %s — falling back "
                "to the Python poller (effective interval ~%.1fs, degraded)",
                path,
                self.fallback_interval_s,
            )
            return False
        try:
            self._watcher_proc = await asyncio.create_subprocess_exec(
                str(path),
                "--interval-ms",
                str(int(self.interval_target_s * 1000)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Captured, not devnull'd: a load failure (missing
                # CAP_BPF) explains an instantly-exiting watcher, and
                # swallowing it made restart loops undiagnosable.
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning(
                "process-ledger: cannot spawn watcher %s (%s) — Python "
                "fallback",
                path,
                exc,
            )
            return False
        self._watcher_reader = asyncio.create_task(
            self._read_watcher_stdout(), name="process-ledger-watcher"
        )
        await self._push_scope()
        return True

    async def _stop_watcher(self) -> None:
        reader, self._watcher_reader = self._watcher_reader, None
        current = asyncio.current_task()
        if reader is not None and reader is not current:
            # Never cancel ourselves: _on_watcher_exit runs INSIDE the
            # reader task, and a self-cancel would raise CancelledError
            # through the restart path (surfacing as a spurious error).
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        proc, self._watcher_proc = self._watcher_proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()

    async def _push_scope(self) -> None:
        """Push the current root-pid scope to the watcher (stdin line)."""
        proc = self._watcher_proc
        if proc is None or proc.stdin is None:
            return
        scope = json.dumps(
            {"type": "scope", "roots": sorted(set(self._roots.values()))}
        )
        try:
            if proc.stdin.is_closing():
                return
            proc.stdin.write(scope.encode() + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            # Watcher died (uvloop surfaces a closed transport as
            # RuntimeError, not BrokenPipeError); the reader task
            # notices the exit and restarts/falls back.
            pass

    async def _read_watcher_stdout(self) -> None:
        """Consume NDJSON events from the watcher, forever.

        On watcher exit: restart (bounded) or switch to Python fallback
        with a recorded coverage gap.
        """
        proc = self._watcher_proc
        assert proc is not None and proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                # Watcher exited (crash or SIGTERM during stop()).
                if self._task is None:
                    return  # stopping; not a crash
                rc = proc.returncode
                stderr_tail = ""
                stderr = getattr(proc, "stderr", None)
                if stderr is not None:
                    try:
                        err = await asyncio.wait_for(
                            proc.stderr.read(), timeout=2
                        )
                        stderr_tail = err.decode("utf-8", "replace").strip()
                    except asyncio.TimeoutError:  # pragma: no cover
                        pass
                if stderr_tail:
                    logger.warning(
                        "process-ledger: watcher exited rc=%s: %s",
                        rc,
                        stderr_tail[-500:],
                    )
                try:
                    await self._on_watcher_exit()
                except Exception:  # pragma: no cover - defensive
                    # Never let a restart-path error kill the reader
                    # task silently (unretrieved-exception noise): log
                    # and keep the fallback bookkeeping intact.
                    logger.exception(
                        "process-ledger: watcher restart path failed"
                    )
                return
            try:
                event = json.loads(line)
            except ValueError:
                continue
            try:
                await self._handle_event(event)
            except Exception:  # pragma: no cover - defensive
                logger.exception("process-ledger: event handler error")

    async def _on_watcher_exit(self) -> None:
        """Crashed watcher: bounded restart, then Python fallback."""
        now = time.time()
        self.gaps.append((now, now))
        self._watcher_restarts = [
            t
            for t in self._watcher_restarts
            if now - t < _WATCHER_RESTART_WINDOW_S
        ]
        if len(self._watcher_restarts) >= _WATCHER_MAX_RESTARTS:
            logger.warning(
                "process-ledger: watcher exited %d times in %.0fs — "
                "switching to Python fallback (degraded interval)",
                _WATCHER_MAX_RESTARTS,
                _WATCHER_RESTART_WINDOW_S,
            )
            self.backend = "python-fallback"
            self.effective_interval_ms = self.fallback_interval_s * 1000.0
            return
        self._watcher_restarts.append(now)
        logger.warning("process-ledger: watcher exited; restarting")
        await self._stop_watcher()
        await self._start_watcher()

    async def _handle_event(self, event: dict) -> None:
        """Join one watcher event to a workspace + persist/attribute."""
        etype = event.get("type")
        if etype == EVT_HEARTBEAT:
            self.effective_interval_ms = float(event.get("interval_ms", 0))
            return
        if etype == EVT_SNAPSHOT_START:
            # Events between the markers are pre-existing processes, not
            # launches under watch; they seed ancestry but write no rows.
            return
        if etype not in (EVT_BIRTH, EVT_EXEC, EVT_REPARENT, EVT_EUID):
            return
        pid = event.get("pid")
        if not isinstance(pid, int):
            return
        ancestry = event.get("ancestry") or []
        ws, chain = self._workspace_for_ancestry(pid, ancestry)
        if ws is None:
            return
        if etype == EVT_EUID:
            # Alarm-surface only for now: log at warning. A dedicated
            # surface lands with the ledger UI.
            logger.warning(
                "process-ledger: euid change in workspace %s pid %d: %s",
                ws,
                pid,
                event,
            )
            return
        if etype in (EVT_REPARENT,):
            return
        ts = float(event.get("ts_realtime", time.time()))
        principal, method = self.attribute_with_ancestry(ancestry + chain, ws)
        if etype in (EVT_BIRTH, EVT_EXEC):
            await self.app.state.model.process_launch.record_launch(
                workspace_id=ws,
                pid=pid,
                ppid=event.get("ppid"),
                uid=event.get("uid"),
                comm=event.get("comm"),
                argv=event.get("argv"),
                started_at=ts,
                principal=principal or "unknown",
                attribution_method=method or ATTR_FALLBACK,
                pane_hint=self.input_hint(ws, ts),
                event_kind=etype,
            )

    def _workspace_for_ancestry(
        self, pid: int, ancestry: list[int]
    ) -> tuple[str | None, list[int]]:
        for a in ancestry:
            ws = self._roots_by_pid.get(a)
            if ws is not None:
                return ws, []
        ws = self._roots_by_pid.get(pid)
        if ws is not None:
            return ws, []
        return None, []

    # -------------------------------------------------- main loop
    async def _run(self) -> None:
        """Supervise: refresh roots/scope, retain, run the fallback poller."""
        try:
            last_prune = 0.0
            while True:
                if not self.enabled:
                    await self.stop()
                    return
                now = time.time()
                if now - last_prune >= 60.0:
                    last_prune = now
                    try:
                        await self.app.state.model.process_launch.prune(
                            keep_rows=int(
                                self.app.state.settings.process_ledger_retention_rows
                            ),
                            keep_seconds=float(
                                self.app.state.settings.process_ledger_retention_seconds
                            ),
                        )
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("process-ledger: prune failed")
                if self.backend == "python-fallback":
                    await self._fallback_poll_once()
                    await asyncio.sleep(self.fallback_interval_s)
                else:
                    await self._refresh_roots()
                    await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            raise

    async def _refresh_roots(self) -> None:
        """Re-resolve container-init pids for running workspaces."""
        registry = self.app.state.container_registry
        for ws_id, state in list(registry.states.items()):
            info = await self.app.state.podman.inspect_container(
                state.container_id
            )
            if info is not None and info.get("State", {}).get("Running"):
                self.set_root(ws_id, int(info["State"].get("Pid", 0)) or 0)
            else:
                self.drop_root(ws_id)
        self.prune_stale_roots()
        self.resolve_anchors()
        self.prune_stale_anchors()
        await self._push_scope()

    async def _fallback_poll_once(self) -> None:
        """One Python-poller pass (degraded backend, budget-derived)."""
        await self._refresh_roots()
        snap = _ProcSnapshot().scan()
        seen = set(snap.entries)
        if self._prev_seen is not None:
            for pid in sorted(seen - self._prev_seen):
                ws, chain = self.workspace_for_pid(pid, snap.entries)
                if ws is None:
                    continue
                ppid, uid = snap.entries[pid]
                ts = time.time()
                principal, method = self.attribute_with_ancestry(chain, ws)
                await self.app.state.model.process_launch.record_launch(
                    workspace_id=ws,
                    pid=pid,
                    ppid=ppid,
                    uid=uid,
                    comm=read_comm(snap.root, pid),
                    argv=read_argv(snap.root, pid),
                    started_at=ts,
                    principal=principal or "unknown",
                    attribution_method=method or ATTR_FALLBACK,
                    pane_hint=self.input_hint(ws, ts),
                    event_kind=EVT_BIRTH,
                )
        self._prev_seen = seen

    # -------------------------------------------------- status
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "effective_interval_ms": self.effective_interval_ms,
            "started_at": self.started_at,
            "roots": len(self._roots),
            "anchors": len(self._anchors),
            "gaps": len(self.gaps),
        }


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("process-ledger: background task failed: %s", exc)


def _read_nspid_tail(hpid: int, root: str = "/proc") -> int | None:
    """Last NSpid entry for a host pid = its in-namespace pid."""
    s = _read_file(os.path.join(root, str(hpid), "status"))
    if s is None:
        return None
    for line in s.splitlines():
        if line.startswith("NSpid:"):
            try:
                return int(line.split()[-1])
            except (IndexError, ValueError):
                return None
    return None


