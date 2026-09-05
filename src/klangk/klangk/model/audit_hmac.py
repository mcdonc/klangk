"""HMAC integrity protection for audit records (#3174).

Each audit row (``container_events``, ``egress_consent``) carries an
HMAC-SHA256 tag computed over a canonical serialization of the row's
data columns at insert time.  The tag lets an external checker (an off-host
backup, an auditor's tool) re-compute it with the same key and detect
that the row was modified after it was written; klangkd itself only
writes tags — it does not verify them.

The HMAC key is ``KLANGKD_AUDIT_HMAC_KEY``. Tagging is **opt-in**:
when the key is unset no HMAC is computed or stored — there is
deliberately no derivation from the JWT secret, because that secret
ships a known insecure dev default and audit integrity must not
silently ride on it. The key is read live from
``app.state.settings`` (reloadable on SIGHUP); rows written while
tagging is disabled carry no tag.

FIPS compatibility: all crypto goes through :mod:`hashlib` /
:mod:`hmac`, which route to the process's OpenSSL — the same boundary
``fips.py`` probes and ``auth.py``'s password KDF uses.
"""

import hashlib
import hmac


def resolve_audit_hmac_key(settings) -> bytes | None:
    """The configured HMAC key bytes, or None when tagging is disabled.

    Opt-in (#3174): with ``KLANGKD_AUDIT_HMAC_KEY`` unset (or empty) no
    HMAC is computed or stored. There is deliberately no derivation
    from the JWT secret — it ships a known insecure dev default, and
    audit integrity must not silently ride on that.
    """
    explicit = settings.audit_hmac_key
    return explicit.encode() if explicit else None


def _canonical_pairs(table: str, row: dict, columns: list[str]) -> bytes:
    """Deterministic serialization: ``table\\0col=len:value\\0col=n\\0...``

    ``None`` is encoded as the bare marker ``n``; every other value is
    ``<len(str(v))>:<str(v)>`` — length-prefixed.  The length prefix
    makes the encoding prefix-free and injective for ANY column
    content, including the attacker-influenced values that come from
    inside untrusted workspaces (``dest_host``, ``process_name``):
    two rows with different field values can never serialize
    identically, so no value — not even a literal ``"n"``, ``\\0``, or
    ``=`` — can impersonate another column's NULL or splice fields.
    The column order is the caller's ``columns`` list (which must match
    the table's canonical column order, excluding the ``hmac`` column
    itself).
    """
    parts = [table]
    for col in columns:
        val = row.get(col)
        if val is None:
            parts.append(f"{col}=n")
        else:
            sv = str(val)
            parts.append(f"{col}={len(sv)}:{sv}")
    return "\0".join(parts).encode()


# --- Container events ---

_CE_HMAC_COLUMNS = [
    "id",
    "workspace_id",
    "event",
    "actor_type",
    "actor_id",
    "cause",
    "container_id",
    "container_role",
    "network_namespace",
    "created_at",
]


def compute_container_event_hmac(settings, row: dict) -> str | None:
    """Compute the HMAC tag for a ``container_events`` row dict, or
    None when no audit HMAC key is configured (tagging disabled)."""
    key = resolve_audit_hmac_key(settings)
    if key is None:
        return None
    payload = _canonical_pairs("container_events", row, _CE_HMAC_COLUMNS)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


# --- Audit events ---

_AE_HMAC_COLUMNS = [
    "id",
    "event",
    "actor_id",
    "actor_email",
    "target_type",
    "target_id",
    "detail",
    "source_ip",
    "user_agent",
    "created_at",
]


def compute_audit_event_hmac(settings, row: dict) -> str | None:
    """Compute the HMAC tag for an ``audit_events`` row dict, or
    None when no audit HMAC key is configured (tagging disabled)."""
    key = resolve_audit_hmac_key(settings)
    if key is None:
        return None
    payload = _canonical_pairs("audit_events", row, _AE_HMAC_COLUMNS)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


# --- Egress consent ---

_EC_HMAC_COLUMNS = [
    "id",
    "workspace_id",
    "dest_host",
    "dest_port",
    "pid",
    "process_name",
    "decision",
    "duration",
    "requested_at",
    "decided_at",
    "decided_by",
    "revoked_at",
    "revoked_by",
]


def compute_egress_consent_hmac(settings, row: dict) -> str | None:
    """Compute the HMAC tag for an ``egress_consent`` row dict, or
    None when no audit HMAC key is configured (tagging disabled)."""
    key = resolve_audit_hmac_key(settings)
    if key is None:
        return None
    payload = _canonical_pairs("egress_consent", row, _EC_HMAC_COLUMNS)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()
