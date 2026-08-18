"""Interactive egress-consent components (#2542 split of consent*.py).

The three former flat modules became single-responsibility submodules:

- :mod:`.egress`      — was ``klangk/consent.py``: the blocked-destination
  receive → persist → expire loop (``EgressConsentMonitor``) plus
  ``workspace_is_interactive``.
- :mod:`.deciders`    — was ``klangk/consent_deciders.py``: the runtime
  registry of live consent deciders (``ConsentDeciderRegistry``).
- :mod:`.coordinator` — was ``klangk/consent_coordinator.py``: decision
  fanout/verdicts/pause (``ConsentCoordinator``).

``__init__`` re-exports the previous public surface so
``from klangk.consent import X`` and ``from klangk import consent;
consent.X`` keep working unchanged.

Patch-target note: monkeypatching module globals must target the defining
submodule (e.g. ``klangk.consent.egress.<name>``); re-exports here are
bindings, not live cells.
"""

from .coordinator import (
    ConsentCoordinator as ConsentCoordinator,
    PAUSE_15M as PAUSE_15M,
    PAUSE_1D as PAUSE_1D,
    PAUSE_1H as PAUSE_1H,
)
from .deciders import ConsentDeciderRegistry as ConsentDeciderRegistry
from .egress import (
    EgressConsentMonitor as EgressConsentMonitor,
    PRUNE_INTERVAL as PRUNE_INTERVAL,
    workspace_is_interactive as workspace_is_interactive,
)
