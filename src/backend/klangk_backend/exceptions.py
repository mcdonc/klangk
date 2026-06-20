"""Domain-specific exceptions for the klangk backend."""


class ConfigurationError(RuntimeError):
    """A required configuration value is missing, invalid, or insecure."""


class TerminalError(RuntimeError):
    """A tmux or terminal operation failed."""


class SendmailError(RuntimeError):
    """The sendmail subprocess exited with a non-zero status."""
