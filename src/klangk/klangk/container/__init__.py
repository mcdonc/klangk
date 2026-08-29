"""Container lifecycle management package (#2542 split of container.py).

The old single 2792-line ``klangk/container.py`` was split into
single-responsibility modules; this package ``__init__`` re-exports the
previous public surface (plus the private names tests and sibling modules
patch/import) so callers keep working unchanged:

- :mod:`.state`    - ``ContainerState``
- :mod:`.ports`    - ``PortAllocator`` + port constants
- :mod:`.identity` - container-name helpers + ``BrowserRouter``
- :mod:`.idle`     - ``IdleMonitor``
- :mod:`.eviction` - ``MemoryPressureEvictor`` (host memory-pressure
  eviction, #2526) + ``read_meminfo``/``available_fraction``
- :mod:`.admission` — ``AdmissionControl`` (start-time host-capacity /
  per-user quota admission, #2525)
- :mod:`.health`   - ``HealthMonitor`` + ``unhealthy_message``
- :mod:`.sidecar`  - network sidecar lifecycle (mixin + constants)
- :mod:`.spec`    - container spec assembly (``ContainerStartSpec``, env/
  mounts/volumes/nix builders, limit resolvers, create kwargs)
- :mod:`.registry` - ``ContainerRegistry`` (lifecycle, bringup, reaps)

Patch-target note: monkeypatching module globals must now target the
defining submodule (e.g. ``klangk.container.registry._pid_alive``,
``klangk.container.sidecar._NETWORK_SIDECAR_READY_TIMEOUT``); re-exports
here are bindings, not live cells.
"""

from ..ssl_trust import ssl_env_vars as ssl_env_vars
from .admission import AdmissionControl as AdmissionControl
from .eviction import (
    MemoryPressureEvictor as MemoryPressureEvictor,
    available_fraction as available_fraction,
    cgroup_memory_headroom as cgroup_memory_headroom,
    macos_available_fraction as macos_available_fraction,
    macos_measure as macos_measure,
    measure_available_fraction as measure_available_fraction,
    parse_vm_stat as parse_vm_stat,
    read_meminfo as read_meminfo,
    vm_stat_page_size as vm_stat_page_size,
)
from .health import (
    HEALTH_MESSAGE_MAX_BYTES as HEALTH_MESSAGE_MAX_BYTES,
    HealthMonitor as HealthMonitor,
    unhealthy_message as unhealthy_message,
)
from .idle import IdleMonitor as IdleMonitor
from .identity import (
    _workspace_container_name as _workspace_container_name,
    _workspace_name_slug as _workspace_name_slug,
    BrowserRouter as BrowserRouter,
)
from .ports import (
    CONTAINER_PORT_START as CONTAINER_PORT_START,
    DEFAULT_PORTS_PER_WORKSPACE as DEFAULT_PORTS_PER_WORKSPACE,
    PortAllocator as PortAllocator,
)
from .registry import (
    ContainerRegistry as ContainerRegistry,
    _pid_alive as _pid_alive,
    _reap_sort_key as _reap_sort_key,
)
from .sidecar import NetworkSidecarMixin as NetworkSidecarMixin
from .spec import (
    ContainerStartSpec as ContainerStartSpec,
    SHARED_HOME as SHARED_HOME,
    SHARED_HOME_NAME as SHARED_HOME_NAME,
)
from .state import ContainerState as ContainerState
