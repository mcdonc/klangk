"""Interactive egress-consent components (#2542 split of consent*.py).

The former flat modules became single-responsibility submodules:

- :mod:`.egress`      — was ``klangk/consent.py``: the egress-consent
  retention sweep (:class:`EgressConsentSweeper`). (The event-intake
  half of the original design was superseded by the sidecar WS +
  coordinator, #2311; the interactivity predicate moved onto the
  coordinator with the single workspace-row read, #3083.)
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

from .coordinator import ConsentCoordinator as ConsentCoordinator
from .deciders import ConsentDeciderRegistry as ConsentDeciderRegistry
from .egress import (
    EgressConsentSweeper as EgressConsentSweeper,
    PRUNE_INTERVAL as PRUNE_INTERVAL,
)
