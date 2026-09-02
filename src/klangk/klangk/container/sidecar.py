"""FQDN network sidecar lifecycle (#2542 split of the old container.py).

``NetworkSidecarMixin`` carries the sidecar half of
``ContainerRegistry``: readiness constants, the per-workspace token file,
the orphan-token sweep, and start/stop/remove of the sidecar container
(#2254). Kept as a mixin (rather than free functions) so every
``registry.<sidecar method>`` call site and test keeps working unchanged.
"""

import asyncio
import logging
import os
import time

from .. import podman
from ..model.container_events import (
    CAUSE_SIDECAR_DEPENDENT,
    CAUSE_SIDECAR_START,
    CAUSE_SIDECAR_STOP,
    EVENT_START,
    EVENT_STOP,
    ROLE_SIDECAR,
)

logger = logging.getLogger(__name__)

# fail-open window. Module-level so the timeout path is unit-testable fast.
NETWORK_SIDECAR_READY_TIMEOUT = 30.0
NETWORK_SIDECAR_READY_POLL = 0.3
_NETWORK_SIDECAR_READY_TOKEN = "dns-proxy listening"
# fwmark the sidecar's proxy stamps on its upstream socket and the entrypoint
# matches in its nat/filter rules (#2264). Single source of truth, passed to the
# sidecar via KLANGKNETWORK_EGRESS_MARK so proxy.py and entrypoint.sh (which
# both default to 75) can't diverge (#2282).
_NETWORK_SIDECAR_MARK = 75
# NFQUEUE queue number the sidecar's interactive-mode consumer binds (#2242).
# Matches the old NFLOG group for familiarity; single source of truth passed to
# both the entrypoint (iptables -j NFQUEUE) and the consumer via env.
_NETWORK_SIDECAR_NFQUEUE = 5139
# How often the idle cleanup loop scans data_dir/ws-tokens/ for orphaned
# sidecar-token files (workspaces whose DB row is gone). Self-throttled so
# the directory + workspace table are not hit on every idle tick (#2309).
ORPHAN_TOKEN_SWEEP_INTERVAL = 300


def _first_container_name(c: dict) -> str:
    names = c.get("Names") or []
    return names[0] if names else ""


def container_ident(c: dict) -> str:
    """Best-effort identifier for a ``podman ps`` container dict.

    ``podman ps --format json`` emits ``Id`` on some versions and ``ID``
    on others; fall back to the first container name when neither is
    present. Shared by the sidecar removal sweep and the registry reaps
    (#2548).
    """
    ident = c.get("Id") or c.get("ID")
    return ident or _first_container_name(c)


def labeled_role_ident(c: dict, role: str) -> str | None:
    """Ident of a listed container when its ``klangk.role`` label is
    *role*, else None (no ident / different role)."""
    labels = c.get("Labels") or {}
    if labels.get("klangk.role") != role:
        return None
    return container_ident(c) or None


def labeled_workspace_id(c: dict) -> str | None:
    """The workspace id a ``podman ps`` dict is labeled for, when the
    container is the workspace container itself (#2915).

    Both the workspace container and its network sidecar carry
    ``klangk.workspace=<id>``; ``klangk.role`` is the discriminator
    (same check as ``_adopt_labeled_container`` and the sidecar
    sweeps). Used by the shutdown/drain orphan sweeps so a labeled
    workspace container stopped without registry tracking still gets
    its stop row — and its sidecar teardown (#2286 semantics).
    """
    labels = c.get("Labels") or {}
    if labels.get("klangk.role") != "workspace":
        return None
    return labels.get("klangk.workspace") or None


def _remaining_sidecar_idents(containers: list[dict]) -> set[str]:
    """Idents of the sidecar-role containers in a listing."""
    return {
        container_ident(c)
        for c in containers
        if (c.get("Labels") or {}).get("klangk.role") == "network-sidecar"
    }


def _forwarded_egress_env() -> list[str]:
    """Operator-tunable sidecar env opts (TTL floor, sweep cadence, debug
    RST, activity gate), forwarded when set in klangkd's environment —
    see the notes in ``_network_sidecar_env``."""
    forwarded = []
    for opt in (
        "KLANGKNETWORK_EGRESS_MIN_TTL",
        "KLANGKNETWORK_EGRESS_SWEEP_INTERVAL",
        "KLANGKNETWORK_EGRESS_DEBUG_RST",
        "KLANGKNETWORK_EGRESS_ACTIVITY_GATE",
    ):
        value = os.environ.get(opt)
        if value:
            forwarded.append(f"{opt}={value}")
    return forwarded


class NetworkSidecarMixin:
    """Sidecar lifecycle methods mixed into ``ContainerRegistry``.

    All state lives on ``self`` (the registry): ``self.app`` for
    settings/podman/workspaces, and the registry's own dicts for
    tracking. The lifecycle-audit hooks (#2915) call
    ``self.record_container_event`` — a ContainerRegistry method, part
    of this mixin's implicit contract (it is only ever mixed into
    ContainerRegistry). See the method docstrings (carried over
    verbatim from the old container.py) for the #NNNN history.
    """

    def network_sidecar_enabled(self) -> bool:
        """Whether the FQDN network sidecar model is configured (#2254).

        Mirrors :meth:`NetFilter.enabled`: the master switch on AND the
        sidecar image set (which ships with a default, so this is True out
        of the box)."""
        return self.app.state.settings.netfilter_enabled and bool(
            self.app.state.settings.network_sidecar_image
        )

    def network_sidecar_name(self, workspace_id: str, slug: str = "") -> str:
        """Derive the network sidecar name (#2254, #2286).

        Carries the slugified workspace name (when there is one) so
        ``podman ps | grep <partial-name>`` surfaces the sidecar next to its
        workspace, and uses the same ``workspace_id[:8]`` tail as the workspace
        container name so an id-prefix grep matches the pair. Removal is by the
        ``klangk.workspace`` label (see :meth:`_remove_network_sidecar`), not by
        name — so a stale slug (after a rename) can't strand an orphan.
        """
        if slug:
            return f"klangk-net-{slug}-{workspace_id[:8]}"
        return f"klangk-net-{workspace_id[:8]}"

    def _sidecar_token_path(self, workspace_id: str) -> str:
        """Host path of the sidecar's bind-mounted workspace-token file."""
        return os.path.join(
            self.app.state.settings.data_dir, "ws-tokens", workspace_id
        )

    def write_sidecar_token(self, workspace_id: str, token: str) -> None:
        """Write the current workspace token to the sidecar's token file.

        The sidecar reads this (read-only bind mount) on each consent POST, so
        refreshing it here -- on launch and on every WS-driven token rotation
        -- keeps the sidecar authenticated without baking a (rotating,
        expiring) token into its env (#2242). The sidecar image lacks the
        workspace image's token-setter, so it can't be exec-pushed like the
        workspace container. Atomic (os.replace) so the sidecar never reads a
        half-written token.
        """
        path = self._sidecar_token_path(workspace_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(token)
        os.replace(tmp, path)

    async def sweep_orphaned_sidecar_tokens(self) -> int:
        """Remove ``ws-tokens/<id>`` files for workspaces that no longer exist.

        The sidecar's workspace-token file is written on every network-sidecar
        start (:meth:`write_sidecar_token`) and intentionally kept across
        stop/restart -- a stopped workspace must keep its token for a later
        restart. Only a final delete (or a crash that skips cleanup) orphans
        the file, so this sweep reclaims them (#2309). Safe: a workspace whose
        row still exists keeps its token; only files with no matching row are
        removed. Returns the number of files removed.
        """
        token_dir = os.path.join(self.app.state.settings.data_dir, "ws-tokens")
        if not os.path.isdir(token_dir):
            return 0
        try:
            names = os.listdir(token_dir)
        except OSError as e:
            logger.warning(
                "Cannot scan %s for orphaned tokens: %s", token_dir, e
            )
            return 0
        try:
            existing = await self.app.state.workspaces.existing_workspace_ids()
        except Exception as e:
            logger.warning("Cannot list workspace ids for token sweep: %s", e)
            return 0
        return self._unlink_orphaned_tokens(token_dir, names, existing)

    def _unlink_orphaned_tokens(
        self, token_dir: str, names: list[str], existing: set[str]
    ) -> int:
        """Unlink token files whose workspace row is gone; transient
        ``.tmp`` files and non-files are skipped. Returns the count."""
        removed = 0
        for name in names:
            path = self._orphaned_token_path(token_dir, name, existing)
            if path is not None and self._unlink_token_file(path, name):
                removed += 1
        return removed

    def _orphaned_token_path(
        self, token_dir: str, name: str, existing: set[str]
    ) -> str | None:
        """Path of one token file to remove — not a transient ``.tmp``,
        a real file, with no workspace row — or None to skip it.

        Skipping ``.tmp`` matters: unlinking one races the writer
        (os.replace would then FileNotFoundError)."""
        if name.endswith(".tmp"):
            return None
        path = os.path.join(token_dir, name)
        if not os.path.isfile(path):
            return None
        if name in existing:
            return None
        return path

    def _unlink_token_file(self, path: str, name: str) -> bool:
        """Unlink one orphaned token file; failures are logged and
        swallowed so one stuck file never aborts the sweep."""
        try:
            os.unlink(path)
            logger.info("Removed orphaned sidecar token %s", name)
            return True
        except OSError as e:
            logger.warning("Could not remove orphaned token %s: %s", name, e)
            return False

    async def start_network_sidecar(
        self,
        workspace_id: str,
        allowed_domains: list[str] | None,
        egress_mode: str = "static",
        rejected_domains: list[str] | None = None,
        publish: list[tuple[int, int]] | None = None,
        slug: str = "",
    ) -> str:
        """Create + start the FQDN network sidecar for a filtered workspace (#2254).

        Returns the network sidecar's container ID (for ``--network container:<id>``).
        Raises ``podman.PodmanError`` on failure — the caller fail-closes (a
        workspace that declared an allow-list never starts unrestricted; #2254
        review B2). The network sidecar gets ``--cap-add NET_ADMIN`` + ``--dns 1.1.1.1``
        (the REDIRECT target the workspace inherits); the proxy forwards to a
        *different* detected upstream (loop avoidance). The allow-list + the
        klangkd backend port are passed via env. ``publish`` (the host ports the
        workspace requested) is published on the sidecar itself (#2267): the
        workspace shares the sidecar's netns, so ``--publish`` is inert on the
        workspace under ``--network container:`` but works on the netns owner,
        forwarding into the shared netns to the workspace's listener — letting
        filtered workspaces host apps.
        """
        image = self.app.state.settings.network_sidecar_image
        if not image:
            raise podman.PodmanError(
                500, "network_sidecar_image is not configured"
            )
        name = self.network_sidecar_name(workspace_id, slug)
        # Pick an upstream that differs from the REDIRECT target (1.1.1.1).
        env, binds = self._network_sidecar_env(
            workspace_id, allowed_domains, rejected_domains
        )

        # Label the network sidecar with this klangk instance so the startup reaper
        # (reap_instance_containers) and the shutdown orphan sweep cull any
        # network sidecar left behind by a failed stop — the same culling workspace
        # containers get (#2254 review). #2342: klangk.managed + klangk.pid also
        # let the dead-owner reap (reap_dead_owner_containers) cull a sidecar
        # whose creating klangkd died uncleanly, the same as a workspace container.
        labels = self._network_sidecar_labels(workspace_id, slug)

        # #2265: a sidecar from a prior generation may linger (an unclean
        # stop, or the workspace container was killed externally so
        # stop_and_remove_container never ran for it). Clear it before create
        # so create_container doesn't collide and the caller doesn't
        # fail-closed. Removal is by the klangk.workspace label (#2286), not
        # the name — the name now carries the workspace-name slug, which may
        # differ from a prior generation's (a rename) and can't be
        # reconstructed from the id alone. The instance reaper is the
        # backstop for orphans; this just keeps a restart from deadlocking.
        # #2676: when the clear fails — a container is still joined to the
        # old sidecar's netns (podman's "dependent containers" refusal) or
        # podman otherwise refuses the removal — refuse here with a clear
        # error instead of swallowing it and letting create_container's
        # --replace collide with the same refusal (a raw 500 + traceback at
        # the caller, guaranteed whenever the dependent survives). Inside
        # the try so the fail-closed warning below logs it like any other
        # sidecar start failure.
        try:
            if not await self._remove_network_sidecar(workspace_id):
                raise podman.PodmanError(
                    500,
                    "cannot remove the existing network sidecar for "
                    f"workspace {workspace_id[:8]}: a container is still "
                    "joined to its network namespace (podman's dependent "
                    "containers refusal) or podman refused the removal; "
                    "stop or remove the workspace's containers before "
                    "starting it again",
                )
            # NET_RAW lets the proxy forge the eager-deny RST directly from
            # the NFQUEUE callback (#2345) so a denied connect() gets
            # ECONNREFUSED at once, independent of the conntrack/retransmit
            # race that made the iptables REJECT rule flaky. Safe to grant
            # here (unlike on the workspace): the sidecar runs only klangk's
            # own proxy, no untrusted code.
            sidecar_kwargs = self._sidecar_create_kwargs(
                env, labels, publish, binds
            )
            cid = await self.app.state.podman.create_container(
                name, image, **sidecar_kwargs
            )
            # Lifecycle audit (#2915 review): record at CREATION, before
            # start/readiness — a sidecar that fails to become ready still
            # exists and its eventual removal records sidecar_stop, so the
            # pair must balance even on the failure path. Sidecar rows are
            # system-caused and never carry a netns owner (the sidecar IS
            # the owner).
            await self.record_container_event(
                workspace_id,
                cid,
                EVENT_START,
                cause=CAUSE_SIDECAR_START,
                container_role=ROLE_SIDECAR,
            )
            await self.start_with_port_conflict_retry(cid, publish or [], name)
            await self._wait_sidecar_proxy_ready(cid, name)
            logger.info(
                "network sidecar started for %s: %s (%s)",
                workspace_id[:8],
                name,
                cid[:12],
            )
            return cid
        except podman.PodmanError as exc:
            logger.warning(
                "network sidecar failed for %s: %s — fail-closed at caller",
                workspace_id[:8],
                exc,
            )
            raise

    def _sidecar_create_kwargs(
        self, env: list[str], labels: dict, publish, binds: list[str]
    ) -> dict:
        """create_container kwargs for the network sidecar."""
        kwargs = dict(
            cap_add=["NET_ADMIN", "NET_RAW"],
            dns=["1.1.1.1"],
            env=env,
            labels=labels,
            publish=publish,
            pull="missing",
        )
        if binds:
            kwargs["binds"] = binds
        return kwargs

    async def _remove_sidecar_attempts(
        self, workspace_id: str, containers: list[dict]
    ) -> list[tuple[str, podman.PodmanError]]:
        """Force-remove each labeled sidecar; the (ident, error) pairs that
        failed."""
        failures: list[tuple[str, podman.PodmanError]] = []
        for c in containers:
            ident = labeled_role_ident(c, "network-sidecar")
            if ident is None:
                continue
            exc = await self._force_remove_sidecar(workspace_id, ident)
            if exc is not None:
                failures.append((ident, exc))
                continue
            # Lifecycle audit (#2915): every successful label-based
            # sidecar removal — workspace teardown, the create path's
            # stale-generation clear, the failure-path teardown — lands
            # here, so this is the sidecar stop choke point.
            await self.record_container_event(
                workspace_id,
                ident,
                EVENT_STOP,
                cause=CAUSE_SIDECAR_STOP,
                container_role=ROLE_SIDECAR,
            )
        return failures

    async def _blocked_sidecar_failures(
        self,
        workspace_id: str,
        failures: list[tuple[str, podman.PodmanError]],
    ) -> list[tuple[str, podman.PodmanError]] | None:
        """Which failed removals still block (a same-label sidecar that
        survived), or None when the state is unknowable (list failure)."""
        remaining = await self._list_workspace_containers(workspace_id)
        if remaining is None:
            return None
        remaining_sidecars = _remaining_sidecar_idents(remaining)
        return [(i, e) for i, e in failures if i in remaining_sidecars]

    def _network_sidecar_labels(
        self, workspace_id: str, slug: str
    ) -> dict[str, str]:
        """Instance/correlation labels for the network sidecar (see the
        inline notes in the original block)."""
        labels = {
            "klangk.managed": "true",
            "klangk.instance": self.app.state.util.instance_id(),
            # The main klangkd daemon process's PID — the liveness signal the
            # dead-owner reap keys on (#2342). Not conmon / the container's
            # PID 1 / a podman subprocess: those die with the daemon anyway.
            "klangk.pid": str(os.getpid()),
            # #2286: a shared klangk.workspace label + a klangk.role label let
            # one `podman ps --filter label=klangk.workspace=<id>` correlate
            # the sidecar with its workspace; the slug is mirrored for
            # exact-match filtering. (Supersedes the old write-only
            # klangk.network-sidecar.)
            "klangk.workspace": workspace_id,
            "klangk.role": "network-sidecar",
            "klangk.workspace-name": slug,
        }
        return labels

    async def _wait_sidecar_proxy_ready(self, cid: str, name: str) -> None:
        """#2277: wait for the sidecar's DNS proxy to be listening before
        returning, so the workspace never joins a netns whose OUTPUT is
        still ACCEPT (entrypoint mid-flight) — a fail-open window. The
        proxy prints "dns-proxy listening" once bound; poll its logs.
        Fail-closed: if the sidecar exits first or the proxy never binds,
        raise so the caller refuses to start the workspace rather than
        run it unfiltered."""
        # #2277: wait for the proxy to be listening before returning, so the
        # workspace never joins a netns whose OUTPUT is still ACCEPT
        # (entrypoint mid-flight) — a fail-open window. The proxy prints
        # "dns-proxy listening" once bound; poll its logs. Fail-closed: if
        # the sidecar exits first or the proxy never binds, raise so the
        # caller refuses to start the workspace rather than run it unfiltered.
        deadline = time.monotonic() + NETWORK_SIDECAR_READY_TIMEOUT
        ready = False
        while time.monotonic() < deadline:
            logs = await self.app.state.podman.container_logs(cid)
            if _NETWORK_SIDECAR_READY_TOKEN in logs:
                ready = True
                break
            status = await self._sidecar_exit_status(cid)
            if status:
                raise podman.PodmanError(
                    500,
                    f"network sidecar {name} exited before the DNS proxy "
                    f"was ready; logs:\n{logs}",
                )
            await asyncio.sleep(NETWORK_SIDECAR_READY_POLL)
        if not ready:
            raise podman.PodmanError(
                500,
                f"network sidecar {name} DNS proxy did not become ready "
                f"within {NETWORK_SIDECAR_READY_TIMEOUT:.0f}s; the "
                "workspace would join an unfiltered netns",
            )

    async def _sidecar_exit_status(self, cid: str) -> str:
        """The sidecar's Status when it already exited/stopped, else ''."""
        state = await self.app.state.podman.inspect_container(cid)
        status = (state or {}).get("State", {}).get("Status", "")
        if status in ("exited", "stopped"):
            return status
        return ""

    def _network_sidecar_env(
        self,
        workspace_id: str,
        allowed_domains: list[str] | None,
        rejected_domains: list[str] | None,
    ) -> tuple[list[str], list[str]]:
        """(env, binds) for the network sidecar container."""
        upstream = self._network_sidecar_upstream()
        env = [
            f"KLANGKNETWORK_EGRESS_ALLOW={','.join(allowed_domains or [])}",
            f"KLANGKNETWORK_EGRESS_REJECT={','.join(rejected_domains or [])}",
            f"KLANGKNETWORK_EGRESS_UPSTREAM={upstream}",
            # The klangkd backend port (LLM proxy + bridge on
            # host.containers.internal). The network sidecar allow-lists it statically
            # — it's a /etc/hosts entry the FQDN proxy can't learn (#2254 B1).
            f"KLANGKNETWORK_EGRESS_BACKEND_PORT={self.app.state.settings.egress_port}",
            # Single source of truth for the fwmark both proxy.py and
            # entrypoint.sh use (#2264, #2282): they default to 75, but pass it
            # explicitly so the two can't diverge.
            f"KLANGKNETWORK_EGRESS_MARK={_NETWORK_SIDECAR_MARK}",
        ]
        # Forward the learned-IP TTL floor + sweep cadence when the operator
        # (or a test) sets them. The sidecar defaults (MIN_TTL=30s, sweep=5s,
        # proxy.py) floor any verdict TTL, so the egress smoketest lowers both
        # to make a short timed verdict expire in seconds. Absent -> defaults.
        # ACTIVITY_GATE (default 60s) likewise: the idle fuzz harness
        # (scripts/fuzz-idle.py, #2514) lowers it so the sidecar's
        # egress-activity bumps are observable at its seconds-scale timeouts.
        env.extend(_forwarded_egress_env())
        binds = []
        # #2242/#2311: consent recording runs for every filtered workspace
        # when the consent stack is wired, regardless of egress_mode -- the
        # mode only affects the recorded decision (static=denied+no-human,
        # interactive=pending), applied by the coordinator over the sidecar
        # WS. The workspace JWT is bind-mounted in and refreshed on rotation
        # (write_sidecar_token), not baked in env (it rotates). The sweeper
        # attribute gates the stack being present at all.
        if getattr(self.app.state, "consent_sweeper", None) is not None:
            consent_env, binds = self._consent_stack_env(workspace_id)
            env.extend(consent_env)
        return env, binds

    def _consent_stack_env(
        self, workspace_id: str
    ) -> tuple[list[str], list[str]]:
        """(env entries, binds) wiring the sidecar to the consent stack."""
        port = self.app.state.settings.egress_port
        env = [
            "KLANGKNETWORK_EGRESS_CONSENT_URL="
            f"http://host.containers.internal:{port}"
            "/internal/egress-consent/events",
            f"KLANGKNETWORK_EGRESS_NFQUEUE_NUM={_NETWORK_SIDECAR_NFQUEUE}",
        ]
        token = self.app.state.auth.create_workspace_token(workspace_id)
        self.write_sidecar_token(workspace_id, token)
        binds = [
            f"{self._sidecar_token_path(workspace_id)}"
            ":/run/klangk/workspace-token:ro"
        ]
        return env, binds

    def _network_sidecar_upstream(self) -> str:
        """The sidecar's upstream resolver (pinned env var or detected)."""
        env_upstream = os.environ.get("KLANGKNETWORK_EGRESS_UPSTREAM")
        if env_upstream:
            return env_upstream
        nf = getattr(self.app.state, "netfilter", None)
        resolvers = nf.resolvers() if nf else []
        return next((r for r in resolvers if r != "1.1.1.1"), "8.8.8.8")

    async def stop_network_sidecar(self, workspace_id: str) -> None:
        """Best-effort remove the network sidecar for a workspace (#2254, #2286).

        Delegates to :meth:`_remove_network_sidecar` (label-based) so a sidecar
        whose name carries a now-stale slug (a renamed or deleted workspace) is
        still found and removed.
        """
        if not self.network_sidecar_enabled():
            return
        await self._remove_network_sidecar(workspace_id)

    async def _remove_network_sidecar(self, workspace_id: str) -> bool:
        """Best-effort remove this workspace's network sidecar by label (#2286).

        Keyed on ``klangk.workspace=<workspace_id>`` +
        ``klangk.role=network-sidecar`` rather than the container name: the name
        now carries the slugified workspace name, which can't be reconstructed
        from the id alone after a rename, a delete (cascade), or a process
        restart (the in-memory set is gone). Label-based removal finds the
        sidecar regardless, leaves the workspace container (role=workspace)
        untouched, and is 404-tolerant.

        Returns True when no same-label sidecar remains — none existed, all
        were removed, or the state is unknowable (a list failure keeps the
        old proceed-anyway semantics). Returns False when a labeled sidecar
        survived removal (#2676): the create path that calls this before
        ``create_container --replace`` refuses to collide with it (podman's
        rm does not override the dependent-containers check), and the stop
        paths log it and fall back to the reaper.

        #2676: a sidecar whose workspace container is still joined to its
        netns (``--network container:<sidecar>``) cannot be removed — podman
        refuses with "has dependent containers" and ``rm -f`` does not
        override that check (the same lesson as the reapers'
        dependents-first ordering, #2476). On that refusal the dependent
        workspace containers of *this* workspace (label-matched, so a
        foreign container joined to the netns is never touched) are removed
        first and the sidecar removal retried once — the create path that
        called this is about to replace those containers anyway.
        """
        containers = await self._list_workspace_containers(workspace_id)
        if containers is None:
            return True
        failures = await self._remove_sidecar_attempts(
            workspace_id, containers
        )
        if not failures:
            return True
        # A removal refused. Re-list: the error may be a benign race (the
        # container vanished between list and rm — "no such container");
        # only a sidecar that is still present can collide with the create
        # that follows, so judge by observable state, not the error text.
        blocked = await self._blocked_sidecar_failures(workspace_id, failures)
        if blocked is None:
            return True
        for ident, exc in blocked:
            logger.warning(
                "network sidecar %s for %s could not be removed: %s",
                ident[:12],
                workspace_id[:8],
                exc,
            )
        return not blocked

    async def _list_workspace_containers(
        self, workspace_id: str
    ) -> list[dict] | None:
        """List this workspace's labeled containers, or None when unknowable.

        Best-effort callers must never raise into their caller (#2286):
        podman errors, OSError, and corrupted ps JSON (``json.loads`` in
        ``list_containers`` — ValueError) all map to None so the caller can
        proceed as before.
        """
        try:
            return await self.app.state.podman.list_containers(
                f"klangk.workspace={workspace_id}"
            )
        except (podman.PodmanError, OSError, ValueError) as exc:
            logger.warning(
                "cannot list containers for network sidecar of %s: %s",
                workspace_id[:8],
                exc,
            )
            return None

    async def _force_remove_sidecar(
        self, workspace_id: str, ident: str
    ) -> podman.PodmanError | None:
        """Force-remove one sidecar container; None on success (#2676).

        On podman's dependent-containers refusal, first remove this
        workspace's own labeled workspace containers (the dependents) and
        retry the sidecar removal once. Returns the final error (if any)
        for the caller to classify against a fresh listing; never raises.
        """
        try:
            await self.app.state.podman.remove_container(ident, force=True)
        except podman.PodmanError as exc:
            if "dependent containers" not in str(exc):
                return exc  # already gone / transient — caller re-lists
            logger.info(
                "sidecar %s for %s has dependent containers; removing the "
                "workspace's own containers to free it (#2676)",
                ident[:12],
                workspace_id[:8],
            )
            await self._remove_dependent_workspace_containers(workspace_id)
            try:
                await self.app.state.podman.remove_container(ident, force=True)
            except podman.PodmanError as retry_exc:
                return retry_exc
        logger.info(
            "network sidecar removed for %s: %s",
            workspace_id[:8],
            ident[:12],
        )
        return None

    async def _remove_dependent_workspace_containers(
        self, workspace_id: str
    ) -> None:
        """Remove this workspace's labeled workspace (dependent) containers.

        Called only when podman refuses to remove a sidecar because a
        dependent container is still joined to its netns (#2676). Only
        containers carrying both ``klangk.workspace=<id>`` and
        ``klangk.role=workspace`` are touched — ours — so a foreign
        dependent stays put and the sidecar refusal surfaces to the caller
        instead. Best-effort: each removal's failure is logged and
        swallowed so one stuck container never aborts the loop.
        """
        containers = await self._list_workspace_containers(workspace_id)
        if containers is None:
            return
        for c in containers:
            ident = labeled_role_ident(c, "workspace")
            if ident is None:
                continue
            try:
                await self.app.state.podman.remove_container(ident, force=True)
                logger.info(
                    "removed dependent workspace container %s to free the "
                    "network sidecar for %s",
                    ident[:12],
                    workspace_id[:8],
                )
                # Lifecycle audit (#2915 review): these are workspace
                # containers destroyed to unblock a sidecar removal —
                # without a row their start rows would dangle forever.
                await self.record_container_event(
                    workspace_id,
                    ident,
                    EVENT_STOP,
                    cause=CAUSE_SIDECAR_DEPENDENT,
                )
            except podman.PodmanError as exc:
                logger.warning(
                    "could not remove dependent workspace container %s for "
                    "%s: %s",
                    ident[:12],
                    workspace_id[:8],
                    exc,
                )
