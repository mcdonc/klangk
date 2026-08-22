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
