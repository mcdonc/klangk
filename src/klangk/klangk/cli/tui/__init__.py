"""Interactive TUI for the klangk client (textual).

Launched by bare ``klangk`` on an interactive terminal (see
``klangk.cli.main._maybe_launch_tui``). Stays within ``klangk.cli``
(isolation rule): only stdlib, third-party deps, and sibling ``cli``
modules.
"""

from .app import KlangkApp, run_tui
from .state import LoginError, ServerInfo, TuiState

__all__ = [
    "KlangkApp",
    "LoginError",
    "ServerInfo",
    "TuiState",
    "run_tui",
]
