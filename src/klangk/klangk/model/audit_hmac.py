"""HMAC integrity protection for audit records (#3174).

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
    explicit = settings.audit_hmac_key
    if explicit:
        return explicit.encode()
    jwt_secret = settings.jwt_secret or ""
    return hmac.new(
        jwt_secret.encode(), _DERIVE_DOMAIN, hashlib.sha256
    ).digest()


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
    """Constant-time comparison; a missing or malformed stored tag
    (NULL, a BLOB, or non-ASCII text — all tampered-column shapes)
    always fails instead of raising (``hmac.compare_digest`` would
    TypeError on those and take down the whole verification pass)."""
    if not isinstance(expected, str) or not expected.isascii():
        return False
    return hmac.compare_digest(expected, computed)


# How many tampered row ids the verification report lists before
# truncating (the full count travels in ``tampered_total``); bounds the
# verify endpoint's response regardless of table size (#3174).
TAMPER_REPORT_CAP = 100


def integrity_report(settings, rows, row_to_dict, compute_hmac) -> dict:
    """Fold audited rows into the verification report (#3174).

    Shared by ``container_events.verify_integrity`` and
    ``egress_consent.verify_integrity``: counts verified / ``no_hmac``
    (NULL tag — pre-migration rows) / tampered rows, and returns the
    first ``TAMPER_REPORT_CAP`` tampered ids plus the full
    ``tampered_total`` and a ``tampered_truncated`` flag, so a large
    corruption cannot blow up the response or the verifier's memory.
    """
    verified = 0
    no_hmac = 0
    tampered_total = 0
    tampered: list[dict] = []
    for row in rows:
        d = row_to_dict(row)
        stored = d.get("hmac")
        if stored is None:
            no_hmac += 1
        elif verify_hmac(stored, compute_hmac(settings, d)):
            verified += 1
        else:
            tampered_total += 1
            if len(tampered) < TAMPER_REPORT_CAP:
                tampered.append(
                    {"id": d["id"], "workspace_id": d["workspace_id"]}
                )
    return {
        "total": len(rows),
        "verified": verified,
        "no_hmac": no_hmac,
        "tampered": tampered,
        "tampered_total": tampered_total,
        "tampered_truncated": tampered_total > len(tampered),
    }
