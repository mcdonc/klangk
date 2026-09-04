"""Domain-specific exceptions for the klangk backend."""

#: Exit status for a deterministic configuration error (sysexits.h
#: ``EX_CONFIG``). The klangkd launcher exits with this status — instead of
#: uvicorn's generic startup-failure status (3) — when the lifespan refused
#: to boot over bad config (e.g. a ``KLANGKD_DEFAULT_PASSWORD`` that violates
#: the password policy). Restarting cannot fix it, so supervisors can treat
#: it as permanent: systemd ``RestartPreventExitStatus=78`` stops the
#: restart loop (#2666).
EX_CONFIG = 78


class ConfigurationError(RuntimeError):
    """A required configuration value is missing, invalid, or insecure.

    Raised by startup validation when the process must refuse to boot. The
    klangkd launcher translates a ``ConfigurationError`` that escapes the
    lifespan into exit status :data:`EX_CONFIG`, so a supervisor can tell
    "config is wrong" apart from a crash (#2666).
    """


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


class WorkspaceCapacityError(RuntimeError):
    """New workspace starts are refused: host capacity is exhausted (#2525).

    Raised at the container-start choke point by admission control —
    either the host-memory fit check (available memory below the
    workspace's resolved memory limit plus the reserve) or the per-user
    running-workspace quota. Like :class:`NodeDrainingError` it is a
    *deterministic, operator-actionable* refusal rather than a runtime
    failure: the API layer translates it to a 503 with a clear detail
    and the WS start paths send an error frame, so clients can render
    "stop a workspace first / free host memory" instead of an opaque
    start failure. The crash-restart loop treats it like any other
    start failure (bounded retries) — capacity may recover on its own
    (the memory-pressure evictor frees idle workspaces, #2526).
    """


class AuditWriteError(RuntimeError):
    """A ``container_events`` audit row could not be written (#3154).

    Security finding V-222486 asked for fail-closed auditing; the
    honest scope is the *interactive* API lifecycle paths only. Raised
    by :meth:`ContainerRegistry.prewrite_audit_event` when
    ``KLANGKD_AUDIT_FAIL_CLOSED`` is on and the audit-before-act row
    for a POST start/stop/restart (or create's eager start, or delete's
    stop) cannot be written: the transition is refused before any side
    effect, and the API layer translates this to a 503.

    Autonomous lifecycle paths (idle timeout, eviction, drain, shutdown
    sweep, crash teardown, boot reaps, logout) never raise it — refusing
    those would keep containers running *and* lose the record, so they
    stay best-effort (logged, counted in ``/health``).
    """
