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

logger = logging.getLogger(__name__)

# fail-open window. Module-level so the timeout path is unit-testable fast.
_NETWORK_SIDECAR_READY_TIMEOUT = 30.0
_NETWORK_SIDECAR_READY_POLL = 0.3
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


def container_ident(c: dict) -> str:
    """Best-effort identifier for a ``podman ps`` container dict.

    ``podman ps --format json`` emits ``Id`` on some versions and ``ID``
    on others; fall back to the first container name when neither is
    present. Shared by the sidecar removal sweep and the registry reaps
    (#2548).
    """
    ident = c.get("Id") or c.get("ID") or ""
    if not ident:
        names = c.get("Names") or []
        ident = names[0] if names else ""
    return ident


class NetworkSidecarMixin:
    """Sidecar lifecycle methods mixed into ``ContainerRegistry``.

    All state lives on ``self`` (the registry): ``self.app`` for
    settings/podman/workspaces, and the registry's own dicts for
    tracking. See the method docstrings (carried over verbatim from
    the old container.py) for the #NNNN history.
    """

    def _network_sidecar_enabled(self) -> bool:
        """Whether the FQDN network sidecar model is configured (#2254).

        Mirrors :meth:`NetFilter.enabled`: the master switch on AND the
        sidecar image set (which ships with a default, so this is True out
        of the box)."""
        return self.app.state.settings.netfilter_enabled and bool(
            self.app.state.settings.network_sidecar_image
        )

    def _network_sidecar_name(self, workspace_id: str, slug: str = "") -> str:
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

    async def _sweep_orphaned_sidecar_tokens(self) -> int:
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
        removed = 0
        for name in names:
            # Skip transient write_sidecar_token temp files: unlinking one
            # races the writer (os.replace would then FileNotFoundError).
            if name.endswith(".tmp"):
                continue
            path = os.path.join(token_dir, name)
            if not os.path.isfile(path):
                continue
            if name in existing:
                continue
            try:
                os.unlink(path)
                removed += 1
                logger.info("Removed orphaned sidecar token %s", name)
            except OSError as e:
                logger.warning(
                    "Could not remove orphaned token %s: %s", name, e
                )
        return removed

    async def _start_network_sidecar(
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
        name = self._network_sidecar_name(workspace_id, slug)
        # Pick an upstream that differs from the REDIRECT target (1.1.1.1).
        # KLANGKNETWORK_EGRESS_UPSTREAM (when set in klangkd's env) pins the
        # sidecar's upstream resolver verbatim, mirroring the MIN_TTL /
        # SWEEP_INTERVAL forwarding below — an operator may want workspaces to
        # use a specific resolver (e.g. a corporate DNS), and the egress
        # smoketest uses it to point the sidecar at a controlled-DNS fixture
        # (#2424) so chosen hostnames resolve to single stable test IPs.
        # Absent -> auto-detect a host resolver that differs from 1.1.1.1.
        env_upstream = os.environ.get("KLANGKNETWORK_EGRESS_UPSTREAM")
        if env_upstream:
            upstream = env_upstream
        else:
            nf = getattr(self.app.state, "netfilter", None)
            resolvers = nf.resolvers() if nf else []
            upstream = next(
                (r for r in resolvers if r != "1.1.1.1"), "8.8.8.8"
            )
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
        for _opt in (
            "KLANGKNETWORK_EGRESS_MIN_TTL",
            "KLANGKNETWORK_EGRESS_SWEEP_INTERVAL",
            "KLANGKNETWORK_EGRESS_DEBUG_RST",
            "KLANGKNETWORK_EGRESS_ACTIVITY_GATE",
        ):
            _v = os.environ.get(_opt)
            if _v:
                env.append(f"{_opt}={_v}")
        binds = []
        # #2242/#2311: consent recording runs for every filtered workspace
        # when the consent stack is wired, regardless of egress_mode -- the
        # mode only affects the recorded decision (static=denied+no-human,
        # interactive=pending), applied by the coordinator over the sidecar
        # WS. The workspace JWT is bind-mounted in and refreshed on rotation
        # (write_sidecar_token), not baked in env (it rotates). The sweeper
        # attribute gates the stack being present at all.
        if getattr(self.app.state, "consent_sweeper", None) is not None:
            port = self.app.state.settings.egress_port
            env.append(
                "KLANGKNETWORK_EGRESS_CONSENT_URL="
                f"http://host.containers.internal:{port}"
                "/internal/egress-consent/events"
            )
            env.append(
                f"KLANGKNETWORK_EGRESS_NFQUEUE_NUM={_NETWORK_SIDECAR_NFQUEUE}"
            )
            token = self.app.state.auth.create_workspace_token(workspace_id)
            self.write_sidecar_token(workspace_id, token)
            binds.append(
                f"{self._sidecar_token_path(workspace_id)}"
                ":/run/klangk/workspace-token:ro"
            )
        # Label the network sidecar with this klangk instance so the startup reaper
        # (reap_instance_containers) and the shutdown orphan sweep cull any
        # network sidecar left behind by a failed stop — the same culling workspace
        # containers get (#2254 review). #2342: klangk.managed + klangk.pid also
        # let the dead-owner reap (reap_dead_owner_containers) cull a sidecar
        # whose creating klangkd died uncleanly, the same as a workspace container.
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
        # #2265: a sidecar from a prior generation may linger (an unclean
        # stop, or the workspace container was killed externally so
        # stop_and_remove_container never ran for it). Clear it before create
        # so create_container doesn't collide and the caller doesn't
        # fail-closed. Removal is by the klangk.workspace label (#2286), not
        # the name — the name now carries the workspace-name slug, which may
        # differ from a prior generation's (a rename) and can't be
        # reconstructed from the id alone. The instance reaper is the
        # backstop for orphans; this just keeps a restart from deadlocking.
        await self._remove_network_sidecar(workspace_id)
        try:
            # NET_RAW lets the proxy forge the eager-deny RST directly from
            # the NFQUEUE callback (#2345) so a denied connect() gets
            # ECONNREFUSED at once, independent of the conntrack/retransmit
            # race that made the iptables REJECT rule flaky. Safe to grant
            # here (unlike on the workspace): the sidecar runs only klangk's
            # own proxy, no untrusted code.
            sidecar_kwargs = dict(
                cap_add=["NET_ADMIN", "NET_RAW"],
                dns=["1.1.1.1"],
                env=env,
                labels=labels,
                publish=publish,
                pull="missing",
            )
            if binds:
                sidecar_kwargs["binds"] = binds
            cid = await self.app.state.podman.create_container(
                name, image, **sidecar_kwargs
            )
            await self._start_with_port_conflict_retry(
                cid, publish or [], name
            )
            # #2277: wait for the proxy to be listening before returning, so the
            # workspace never joins a netns whose OUTPUT is still ACCEPT
            # (entrypoint mid-flight) — a fail-open window. The proxy prints
            # "dns-proxy listening" once bound; poll its logs. Fail-closed: if
            # the sidecar exits first or the proxy never binds, raise so the
            # caller refuses to start the workspace rather than run it unfiltered.
            deadline = time.monotonic() + _NETWORK_SIDECAR_READY_TIMEOUT
            ready = False
            while time.monotonic() < deadline:
                logs = await self.app.state.podman.container_logs(cid)
                if _NETWORK_SIDECAR_READY_TOKEN in logs:
                    ready = True
                    break
                state = await self.app.state.podman.inspect_container(cid)
                status = (state or {}).get("State", {}).get("Status", "")
                if status in ("exited", "stopped"):
                    raise podman.PodmanError(
                        500,
                        f"network sidecar {name} exited before the DNS proxy "
                        f"was ready; logs:\n{logs}",
                    )
                await asyncio.sleep(_NETWORK_SIDECAR_READY_POLL)
            if not ready:
                raise podman.PodmanError(
                    500,
                    f"network sidecar {name} DNS proxy did not become ready "
                    f"within {_NETWORK_SIDECAR_READY_TIMEOUT:.0f}s; the "
                    "workspace would join an unfiltered netns",
                )
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

    async def _stop_network_sidecar(self, workspace_id: str) -> None:
        """Best-effort remove the network sidecar for a workspace (#2254, #2286).

        Delegates to :meth:`_remove_network_sidecar` (label-based) so a sidecar
        whose name carries a now-stale slug (a renamed or deleted workspace) is
        still found and removed.
        """
        if not self._network_sidecar_enabled():
            return
        await self._remove_network_sidecar(workspace_id)

    async def _remove_network_sidecar(self, workspace_id: str) -> None:
        """Best-effort remove this workspace's network sidecar by label (#2286).

        Keyed on ``klangk.workspace=<workspace_id>`` +
        ``klangk.role=network-sidecar`` rather than the container name: the name
        now carries the slugified workspace name, which can't be reconstructed
        from the id alone after a rename, a delete (cascade), or a process
        restart (the in-memory set is gone). Label-based removal finds the
        sidecar regardless, leaves the workspace container (role=workspace)
        untouched, and is 404-tolerant.
        """
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.workspace={workspace_id}"
            )
        except (podman.PodmanError, OSError, ValueError) as exc:
            # ValueError: corrupted ps JSON (json.loads in list_containers) —
            # best-effort removal must never raise into the caller.
            logger.warning(
                "cannot list containers to remove network sidecar for %s: %s",
                workspace_id[:8],
                exc,
            )
            return
        for c in containers:
            labels = c.get("Labels") or {}
            if labels.get("klangk.role") != "network-sidecar":
                continue
            ident = container_ident(c)
            if not ident:
                continue
            try:
                await self.app.state.podman.remove_container(ident, force=True)
                logger.info(
                    "network sidecar removed for %s: %s",
                    workspace_id[:8],
                    ident[:12],
                )
            except podman.PodmanError:
                pass  # already gone — expected
