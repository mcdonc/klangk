"""Module-level WebSocketState singleton (#1464).

Breaks the ``container`` ↔ ``wshandler`` circular import: both sides
import this module instead of each other. ``wshandler.session`` writes
the singleton at import time; ``container.HealthMonitor`` reads it.

The singleton moves to ``app.state.connections`` once all callers are
migrated (Slice 2d, #1465). Until then this module is the single
source of truth for the instance.
"""

#: The process-wide ``WebSocketState`` instance. Set once by
#: ``wshandler.session`` at import time; read by ``container.py``
#: and ``main.py``.
state = None
