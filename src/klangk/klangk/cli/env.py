"""Client-side environment variable entry validation."""


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
