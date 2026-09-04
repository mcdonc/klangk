"""HMAC integrity protection for audit records (STIG V-222507, #3174).

Each audit row (``container_events``, ``egress_consent``) carries an
HMAC-SHA256 tag computed over a canonical serialization of the row's
data columns at insert time.  Verification re-computes the tag and
compares; a mismatch means the row was modified after it was written.

The HMAC key is ``KLANGKD_AUDIT_HMAC_KEY``.  When unset the key is
derived from the server's JWT secret (``KLANGKD_JWT_SECRET``) via a
one-round HMAC-SHA256 domain separation so integrity is on by default
without requiring a second secret.  The key is read live from
``app.state.settings`` (reloadable on SIGHUP).

FIPS compatibility: all crypto goes through :mod:`hashlib` /
:mod:`hmac`, which route to the process's OpenSSL — the same boundary
``fips.py`` probes and ``auth.py``'s password KDF uses.
"""

import hashlib
import hmac


# Domain-separation tag used when deriving the audit HMAC key from the
# JWT secret (the default — no explicit KLANGKD_AUDIT_HMAC_KEY).
_DERIVE_DOMAIN = b"klangk-audit-hmac-v1"


def _resolve_key(settings) -> bytes:
    """Return the HMAC key bytes, derived or explicit."""
    explicit = getattr(settings, "audit_hmac_key", None)
    if explicit:
        return explicit.encode()
    jwt_secret = settings.jwt_secret or ""
    return hmac.new(
        jwt_secret.encode(), _DERIVE_DOMAIN, hashlib.sha256
    ).digest()


def _canonical_pairs(table: str, row: dict, columns: list[str]) -> bytes:
    """Deterministic serialization: ``table\\0col=val\\0col=val\\0...``

    None is encoded as the literal string ``<nil>``; everything else is
    ``str(value)``.  The column order is the caller's ``columns`` list
    (which must match the table's canonical column order, excluding the
    ``hmac`` column itself).
    """
    parts = [table]
    for col in columns:
        val = row.get(col)
        parts.append(f"{col}={'<nil>' if val is None else val}")
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


def compute_container_event_hmac(settings, row: dict) -> str:
    """Compute the HMAC tag for a ``container_events`` row dict."""
    key = _resolve_key(settings)
    payload = _canonical_pairs("container_events", row, _CE_HMAC_COLUMNS)
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


def compute_egress_consent_hmac(settings, row: dict) -> str:
    """Compute the HMAC tag for an ``egress_consent`` row dict."""
    key = _resolve_key(settings)
    payload = _canonical_pairs("egress_consent", row, _EC_HMAC_COLUMNS)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_hmac(expected: str | None, computed: str) -> bool:
    """Constant-time comparison; a missing (NULL) stored tag always fails."""
    if expected is None:
        return False
    return hmac.compare_digest(expected, computed)
