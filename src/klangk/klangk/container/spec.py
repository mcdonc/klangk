"""Container spec assembly (#2566 split of registry.py).

The "what exactly do we ask podman to create" cluster, extracted from
``container/registry.py``:

- :class:`ContainerStartSpec` — the parameter object shared by
  ``ContainerRegistry.start_container`` and its under-lock inner
  implementation, replacing the duplicated ~20-parameter signatures
  (adding a start parameter is now a single field).
- env / mount / volume / nix builders (``build_env``, ``build_mounts``,
  ``ensure_volumes``, ``nix_binds``).
- the per-workspace resource-limit resolvers (``resolve_*``).
- ``build_create_kwargs`` — the ``podman create`` kwargs builder.

Functions take the app-state object and read settings-derived values
live off ``app.state`` (the app-ownership rule, #1608).
"""

import logging
import os
from dataclasses import dataclass

from .. import workspace_settings as ws_settings
from ..podman import (
    SHARED_HOME as SHARED_HOME,
    SHARED_HOME_NAME as SHARED_HOME_NAME,
)
from ..ssl_trust import SSL_MOUNT_DEST as _SSL_MOUNT_DEST, ssl_env_vars
from .ports import CONTAINER_PORT_START, DEFAULT_PORTS_PER_WORKSPACE

logger = logging.getLogger(__name__)

# The single home every connection shares when a workspace has
# ``per_handle_home = false`` (#2169 chunk 2, #2720), and the HOME the
# ``service`` tmux session is pinned to under BOTH layouts (#2717):
# ``/home/klangk``, materialized on the host before ``podman start``
# (``ensure_shared_home_dir``) and populated from /etc/skel at every
# fresh container create by ``ensure_shared_home`` — the image's uid
# 1000 has no usable ``/home/klangk`` (its passwd home is ``/home``
# itself, and the home volume mounts at ``/home`` shadowing whatever
# the image built there), which is why the skel copy is needed at
# all). Lives here — the container
# filesystem-layout module — so ``workspaces``/``wshandler``/``health``
# can import it without a cycle through the ``container`` package.
#
# The agent identity's handle is fixed to this name (#2718, immutable),
# so every site that used to recompute ``/home/{agent_handle()}`` from
# the DB reads these constants instead — one source of truth for both
# the shared-layout home and the agent/service-session home (#2720
# review: "make them both the same"). The definitions live in
# ``podman.py`` (below this package, where the ``work_dir`` default
# needs them — importing them from here would drag
# ``container/__init__`` into podman's module init and close the
# podman↔container import cycle); re-exported here so the
# ``workspaces``/``wshandler``/``health`` import sites keep working
# without a cycle through the ``container`` package. Re-exported from
# ``podman.py`` at the imports above (same-name ``as`` marks the
# intentional re-export).
_VALID_PULL_POLICIES = {"never", "missing", "always", "newer"}


def _split_csv(raw: str | None) -> list[str]:
    """Split a comma-separated settings string into stripped parts.

    Shared by the registry's CSV-shaped settings accessors
    (``allowed_images``, ``allowed_mount_roots``, DNS configs) and the
    create-kwargs builder (#2566). Empty/absent -> ``[]``.
    """
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_named_volume(source: str) -> bool:
    """A mount source with no '/' that doesn't start with '.' is a volume."""
    return "/" not in source and not source.startswith(".")


@dataclass(slots=True)
class ContainerStartSpec:
    """Parameters for :meth:`ContainerRegistry.start_container` (#2566).

    Collapses the previously duplicated ``start_container`` /
    ``_start_container_inner`` signature pair into one shared shape:
    the public method takes (and forwards) this spec, the inner
    under-lock implementation unpacks it once, and adding a start
    parameter becomes a single field here instead of a two-signature
    edit. (The pre-#2725 ``host_path`` field — the ``home/work``
    subtree — is gone; mounts are driven by ``home_path`` /
    ``config_path`` / ``extra_mounts``.)
    """

    workspace_id: str
    home_path: str
    existing_container_id: str | None = None
    num_ports: int = DEFAULT_PORTS_PER_WORKSPACE
    hosting_hostname: str | None = None
    hosting_proto: str | None = None
    hosting_base_path: str | None = None
    image: str | None = None
    config_path: str | None = None
    extra_mounts: list[str] | None = None
    extra_env: dict[str, str] | None = None
    user_id: str | None = None
    health_check: str | None = None
    setup_state: str | None = None
    service_command: str | None = None
    allowed_domains: list[str] | None = None
    rejected_domains: list[str] | None = None
    workspace_settings: dict | None = None
    egress_mode: str = "static"
    # Home layout (#2169 chunk 2, #2720): True -> per-handle
    # /home/{handle} symlinks; False -> the single shared /home/klangk.
    # Traveled on the spec so ContainerState carries it for the health
    # monitor (same pattern as health_check/owner_id/setup_state).
    per_handle_home: bool = True


def image_pull_policy(app) -> str:
    """Resolve the workspace-image pull policy from settings."""
    policy = app.state.settings.image_pull_policy
    if policy not in _VALID_PULL_POLICIES:
        logger.warning(
            "Invalid KLANGKD_IMAGE_PULL_POLICY=%r (valid: %s); using 'never'.",
            policy,
            ", ".join(sorted(_VALID_PULL_POLICIES)),
        )
        return "never"
    return policy


# --- per-workspace resource limits (#864 / #34) ---


def _ws_setting(workspace_settings: dict | None):
    """Wrap a workspace-settings dict for the ``ws_settings`` resolvers.

    The resolvers (#864) take ``{"settings": ...}``; every per-workspace
    limit below resolves with the same wrapper shape, so build it once.
    """
    return {"settings": workspace_settings}


def resolve_cpu_limit(app, workspace_settings: dict | None) -> float | None:
    """Per-workspace CPU limit (``--cpus``), #864 / #34.

    Workspace override > deploy default > None (no flag, unbounded).
    Override semantics (#34): a deploy-wide value is a plain default,
    not a cap or floor — a creator may go larger *or* smaller, applied
    as-is with no clamping.
    """
    return ws_settings.resolve_cpu_limit(
        _ws_setting(workspace_settings),
        app.state.settings.container_cpu_limit,
    )


def resolve_memory_limit(app, workspace_settings: dict | None) -> str | None:
    """Per-workspace memory limit (``--memory``), #864 / #34."""
    return ws_settings.resolve_memory_limit(
        _ws_setting(workspace_settings),
        app.state.settings.container_memory_limit,
    )


def resolve_pids_limit(app, workspace_settings: dict | None) -> int | None:
    """Per-workspace PIDs limit (``--pids-limit``), #864 / #34."""
    return ws_settings.resolve_pids_limit(
        _ws_setting(workspace_settings),
        app.state.settings.container_pids_limit,
    )


def resolve_tmp_size(app, workspace_settings: dict | None) -> str | None:
    """Per-workspace ``/tmp`` tmpfs size (``--tmpfs /tmp:...,size=<n>``).

    #2378: same precedence as the other resolvers (workspace override >
    deploy default > none). ``None`` means mount ``/tmp`` with no
    explicit ``size=`` option (podman sizes it at half of RAM).
    """
    return ws_settings.resolve_tmp_size(
        _ws_setting(workspace_settings),
        app.state.settings.container_tmp_size,
    )


def _hosting_floor(
    app,
    hosting_hostname: str | None,
    hosting_proto: str | None,
    hosting_base_path: str | None,
) -> tuple[str, str, str]:
    """Fill any omitted hosting value with the resolver's floor
    (``derive_hosting_info`` with no request)."""
    if (
        hosting_hostname is None
        or hosting_proto is None
        or hosting_base_path is None
    ):
        h, p, b = app.state.util.derive_hosting_info(None, None)
        # Use ``is None`` (not ``or``): an explicit empty base_path
        # (root deployment) is a legitimate value that must survive,
        # not be clobbered by the resolved floor.
        if hosting_hostname is None:
            hosting_hostname = h
        if hosting_proto is None:
            hosting_proto = p
        if hosting_base_path is None:
            hosting_base_path = b
    return hosting_hostname, hosting_proto, hosting_base_path


def _append_hosted_env(
    app,
    env_vars: list[str],
    host_ports: list[int],
    hosting_hostname: str,
    hosting_proto: str,
    hosting_base_path: str,
) -> None:
    """Hosted-app serving env. Omitted entirely when the workspace has
    no host ports (KLANGKD_HOSTED_PORTS_PER_WORKSPACE=0, or a
    per-workspace value of 0): KLANGKWS_PORT_MAPPINGS absent makes
    klangk-hosted-url / get_hosted_url error out cleanly, and the
    KLANGKWS_HOSTING_* vars are meaningless without hosting. #1237
    Also omitted in headless deployments (KLANGKD_PORT unset, #2732):
    /hosted/ is served by the browser listener, which headless mode
    does not render, so any hosted URL baked now would be dead on
    arrival — the same clean-error outcome as the cap-0 case."""
    if host_ports and app.state.settings.port is not None:
        mappings = [
            f"{CONTAINER_PORT_START + i}:{hp}"
            for i, hp in enumerate(host_ports)
        ]
        env_vars.append(f"KLANGKWS_PORT_MAPPINGS={','.join(mappings)}")
        env_vars.append(f"KLANGKWS_HOSTING_HOSTNAME={hosting_hostname}")
        env_vars.append(f"KLANGKWS_HOSTING_PROTO={hosting_proto}")
        env_vars.append(f"KLANGKWS_HOSTING_BASE_PATH={hosting_base_path}")


def _append_feature_env(
    app, env_vars: list[str], extra_env: dict[str, str] | None
) -> None:
    """Feature flags, then caller extras (an extra wins by appending
    later)."""
    for k, v in app.state.features.container_env().items():
        env_vars.append(f"{k}={v}")

    if extra_env:
        for k, v in extra_env.items():
            env_vars.append(f"{k}={v}")


def build_env(
    app,
    workspace_id: str,
    host_ports: list[int],
    hosting_hostname: str | None,
    hosting_proto: str | None,
    hosting_base_path: str | None,
    agent_home: str,
    extra_env: dict[str, str] | None,
    ssl_dir: str | None = None,
) -> list[str]:
    """Build the container environment variable list.

    ``hosting_hostname``/``hosting_proto``/``hosting_base_path`` are
    optional: callers with a live request pass the values they derived
    from its headers (``wshandler.connection``), and callers without one
    (``start_workspace`` — autostart/create, no connection yet) pass
    ``None``. Resolving the floor here, at the single choke point, means
    no start path can bypass the override: when a caller omits them we
    derive the env / bare-localhost floor via ``derive_hosting_info``
    (the same resolver the request paths use), so a deployer's
    ``KLANGKD_HOSTING_HOSTNAME`` is honored on every start — eager or not.
    """
    hosting_hostname, hosting_proto, hosting_base_path = _hosting_floor(
        app, hosting_hostname, hosting_proto, hosting_base_path
    )
    env_vars: list[str] = []
    egress_port = app.state.settings.egress_port
    proxy_url = f"http://host.containers.internal:{egress_port}/llm-proxy"
    env_vars.append(f"KLANGKWS_LLM_PROXY_URL={proxy_url}")
    env_vars.append("PI_SKIP_VERSION_CHECK=1")
    logger.info("Container LLM proxy: %s", proxy_url)

    # Hosted-app serving env. Omitted entirely when the workspace has
    # no host ports (KLANGKD_HOSTED_PORTS_PER_WORKSPACE=0, or a
    # per-workspace value of 0): KLANGKWS_PORT_MAPPINGS absent makes
    # klangk-hosted-url / get_hosted_url error out cleanly, and the
    # KLANGKWS_HOSTING_* vars are meaningless without hosting. #1237
    # Also omitted in headless deployments (KLANGKD_PORT unset, #2732):
    # /hosted/ is served by the browser listener, which headless mode
    # does not render, so any hosted URL baked now would be dead on
    # arrival — the same clean-error outcome as the cap-0 case.
    _append_hosted_env(
        app,
        env_vars,
        host_ports,
        hosting_hostname,
        hosting_proto,
        hosting_base_path,
    )
    env_vars.append(f"KLANGKWS_WORKSPACE_ID={workspace_id}")
    env_vars.append(f"KLANGKWS_AGENT_HOME={agent_home}")
    env_vars.append(
        f"KLANGKWS_BRIDGE_URL=http://host.containers.internal:{egress_port}"
    )
    # #2153: Set USER/LOGNAME so tools inside the container (git,
    # shell prompts, sudo audit, Pi agent identity) see the correct
    # UNIX user — containers have no login process to set these.
    env_vars.append("USER=klangk")
    env_vars.append("LOGNAME=klangk")
    banner = app.state.settings.terminal_banner or ""
    if banner:
        env_vars.append(f"KLANGKWS_TERMINAL_BANNER={banner}")

    # Runtime SSL/CA trust (#1181): point OpenSSL/Python/curl/Node
    # at the bundle the entrypoint builds from the mounted certs.
    # Appended before feature/extra env so a deployer can override if
    # ever needed. Emitted only when a trustable cert dir is present.
    env_vars.extend(ssl_env_vars(ssl_dir))

    _append_feature_env(app, env_vars, extra_env)
    return env_vars


def build_mounts(
    home_path: str,
    config_path: str | None,
    extra_mounts: list[str] | None,
    ssl_dir: str | None = None,
) -> list[str]:
    """Build the bind-mount list for the container."""
    binds = [f"{home_path}:/home"]
    if config_path:
        binds.append(f"{config_path}:/opt/klangk/config:ro")
    if ssl_dir:
        # Read-only mount of deployer CA certs (#1181).
        binds.append(f"{ssl_dir}:{_SSL_MOUNT_DEST}:ro")
    binds += extra_mounts or []
    return binds


async def _ensure_named_volume(app, user_id, podman, source: str) -> None:
    """Create a missing named volume (instance-labelled, owner-tagged) or
    validate an existing one belongs to this instance and user."""
    info = await podman.inspect_volume(source)
    if info is None:
        labels = {
            "klangk.managed": "true",
            "klangk.instance": app.state.util.instance_id(),
        }
        if user_id:
            labels["klangk.user-id"] = user_id
        await podman.create_volume(source, labels)
        return
    vol_labels = info.get("Labels") or {}
    if vol_labels.get("klangk.instance") != app.state.util.instance_id():
        raise ValueError(
            f"Volume {source!r} is not managed by this klangk instance"
        )
    vol_owner = vol_labels.get("klangk.user-id")
    if vol_owner and user_id and vol_owner != user_id:
        raise ValueError(f"Volume {source!r} belongs to another user")


async def ensure_volumes(
    app,
    extra_mounts: list[str] | None,
    user_id: str | None,
    podman,
) -> None:
    """Create named volumes and validate bind-mount sources."""
    if not extra_mounts:
        return
    for mount_spec in extra_mounts:
        source = mount_spec.split(":")[0]
        if _is_named_volume(source):
            await _ensure_named_volume(app, user_id, podman, source)
        elif not os.path.exists(source):
            raise ValueError(f"Bind mount source does not exist: {source}")


async def nix_binds(
    app, workspace_id: str, workspace_settings: dict | None
) -> tuple[list[str], list[str]]:
    """Bind specs + env for the workspace's per-workspace /nix (#2201), or ([], []).

    Only when the workspace has its per-workspace ``nix`` setting enabled
    (#2202) AND the feature is armed — a backend configured (``nix_seed``,
    #2219/#2220) and ``nix_enabled`` on (#2560) — does
    ensure_workspace_nix provision the per-workspace
    /nix and return a mountpoint; the mountpoint's /nix + nix.conf are
    bind-mounted into the container, and KLANGKWS_NIX=1 is set so the
    baked /etc/profile.d activation (see src/containers/workspace/Dockerfile)
    puts nix on PATH by default. Image selection is untouched.

    Returns ``(binds, env_extras)``.
    """
    if not (workspace_settings or {}).get("nix"):
        return [], []
    mountpoint = await app.state.nix.ensure_workspace_nix(workspace_id)
    if not mountpoint:
        return [], []
    return (
        [
            f"{mountpoint}/nix:/nix",
            f"{mountpoint}/nix.conf:/etc/nix/nix.conf:ro",
        ],
        # Signal the baked profile.d activation that the feature mounted
        # /nix (checked alongside /nix/nix-profile presence).
        ["KLANGKWS_NIX=1"],
    )


def build_create_kwargs(
    app,
    *,
    workspace_id: str,
    iid: str,
    slug: str,
    binds: list[str],
    env_vars: list[str],
    publish: list[tuple[int, int]],
    workspace_settings: dict | None,
) -> dict:
    """Build the ``podman create`` kwargs for a workspace container.

    Everything static that every workspace create shares: labels,
    bind/tmpfs mounts, DNS, resource limits, pull policy. The
    egress-model-specific pieces (``network``, cap add/drop, port
    moves to the sidecar) are layered on by the caller — see
    ``ContainerRegistry._start_container_inner``.
    """
    # #2378: per-workspace /tmp tmpfs size. Default (``2g``) preserves
    # the pre-#2378 mount; ``None`` (explicit unset) -> no ``size=``
    # option, so podman sizes it at half of RAM.
    tmp_size = resolve_tmp_size(app, workspace_settings)
    tmp_opts = "rw,exec,nosuid"
    if tmp_size:
        tmp_opts += f",size={tmp_size}"

    return dict(
        labels={
            "klangk.managed": "true",
            "klangk.instance": iid,
            # The main klangkd daemon process's PID — the liveness signal
            # the dead-owner reap keys on (#2342).
            "klangk.pid": str(os.getpid()),
            # #2286: shared label + role so one `podman ps --filter
            # label=klangk.workspace=<id>` correlates the workspace with its
            # network sidecar; the slug mirrors the name for exact-match
            # filtering. (Supersedes the old write-only klangk.workspace-id.)
            "klangk.workspace": workspace_id,
            "klangk.role": "workspace",
            "klangk.workspace-name": slug,
        },
        binds=binds,
        tmpfs={
            "/tmp": tmp_opts,
            "/run": "rw,noexec,nosuid,size=256m",
            "/var/log": "rw,noexec,nosuid,size=256m",
        },
        publish=publish,
        add_hosts=["host.containers.internal:host-gateway"],
        dns=_split_csv(app.state.settings.dns_servers) or None,
        dns_search=_split_csv(app.state.settings.dns_search) or None,
        env=env_vars,
        init=True,
        interactive=True,
        userns=app.state.settings.userns,
        cpus=resolve_cpu_limit(app, workspace_settings),
        memory=resolve_memory_limit(app, workspace_settings),
        pids_limit=resolve_pids_limit(app, workspace_settings),
        pull=image_pull_policy(app),
    )
