"""Client-side mount spec format validation.

Server-side enforcement (protected paths, allowed roots) is handled
by the backend.  This module only catches obvious format errors
before sending the spec to the API.
"""

_VALID_MOUNT_OPTIONS = {
    "ro",
    "rw",
    "z",
    "Z",
    "nocopy",
    "consistent",
    "cached",
    "delegated",
}


def validate_env_entry(spec: str) -> str | None:
    """Validate a ``KEY=VALUE`` environment variable entry.

    Returns None if valid, or an error message string if invalid.
    Mirrors the Flutter ``CreateWorkspaceDialog`` rule: the entry must
    contain ``=`` and have a non-empty key (the part before the first
    ``=``). The value may be empty.
    """
    if "=" not in spec:
        return f"Invalid env {spec!r}: expected KEY=VALUE"
    key, _, _ = spec.partition("=")
    if not key:
        return f"Invalid env {spec!r}: key cannot be empty"
    return None


def validate_mount_spec(spec: str) -> str | None:
    """Validate a container mount spec string.

    Returns None if valid, or an error message string if invalid.
    Valid forms: source:dest or source:dest:options
    The container path (dest) must be absolute.
    """
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        return (
            f"Invalid mount {spec!r}: "
            "expected source:dest or source:dest:options"
        )
    source, dest = parts[0], parts[1]
    if not source:
        return f"Invalid mount {spec!r}: source is empty"
    if not dest.startswith("/"):
        return (
            f"Invalid mount {spec!r}: "
            "container path must be absolute (start with /)"
        )
    if len(parts) == 3:
        options = parts[2]
        for opt in options.split(","):
            if opt and opt not in _VALID_MOUNT_OPTIONS:
                return f"Invalid mount {spec!r}: unknown option {opt!r}"
    return None
