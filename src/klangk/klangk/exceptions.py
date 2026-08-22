"""Domain-specific exceptions for the klangk backend."""


class ConfigurationError(RuntimeError):
    """A required configuration value is missing, invalid, or insecure."""


class TerminalError(RuntimeError):
    """A tmux or terminal operation failed."""


class ContainerGoneError(TerminalError):
    """The container a terminal/tmux operation targeted no longer exists.

    Raised when ``podman exec`` reports the container is gone (e.g.
    "no container with name or ID ... found"). Distinct from a plain
    tmux failure so callers can treat a recycled container as an
    expected, recoverable condition instead of logging a traceback
    (#2178).
    """


class SendmailError(RuntimeError):
    """The sendmail subprocess exited with a non-zero status."""


class NodeDrainingError(RuntimeError):
    """New workspace starts are refused: a graceful restart (#2527).

    Raised at the container-start choke point while the in-memory drain
    flag is set (a SIGHUP graceful restart is in progress). Existing
    workspaces keep running; only fresh container creation is blocked.
    The API layer translates it to a 503 with a clear detail, the WS
    start paths send an error frame, and the crash restart loop
    abandons quietly.
    """
