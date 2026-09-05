# Audit Record Integrity

klangk can tag its audit records — the `container_events` container
lifecycle history, the `egress_consent` egress verdict trail, and the
`audit_events` identity/privilege stream (#3205) — with
an HMAC-SHA256 tag written at the same time as the row. The tag lets an
**external** checker prove, later and off-host, that a row was not
modified after klangk wrote it. klangkd itself only _writes_ tags; it
does not expose a verification endpoint or command. Verification is
done by whoever holds the key and a copy of the database — typically an
off-host audit backup pipeline ([Backup and Restore](backup-restore.md)).

## Enablement

Tagging is **opt-in** via [`KLANGKD_AUDIT_HMAC_KEY`](environment.md)
(also settable as `audit_hmac_key` in the
[configuration file](klangkd-config.md)):

- **Key set (nonempty)** — every new audit row is written with a tag;
  every mutation of an `egress_consent` row (decide, revoke, expire)
  re-computes it. Deleting the user who made a verdict also re-stamps
  the affected rows in the same transaction (the `decided_by` /
  `revoked_by` FK sets them NULL) — routine offboarding never shows up
  as a mismatch.
- **Key unset (the default)** — no tag is computed or stored. Rows
  carry `NULL` in the `hmac` column.

There is deliberately no derivation from `KLANGKD_JWT_SECRET`: that
secret ships a known insecure dev default, and audit integrity should
not silently ride on it. Choose a long random value for the audit key
(it never needs to sign anything time-sensitive, so length beats
cleverness):

```bash
KLANGKD_AUDIT_HMAC_KEY=$(openssl rand -hex 32)
```

The key is read live from settings, so a SIGHUP reload picks up a
change without a restart.

### Rotation

Changing the key does not rewrite old rows. Tags written under the old
key will not match the new key, so an offsite check that mixes keys
reports those rows as mismatches. Export (and check) the rows you care
about **before** rotating, or record the rotation point (timestamp /
max row id) and verify pre-rotation rows with the old key.

## What is tagged

All three tables live in `<data_dir>/klangk.db`. `container_events` and
`egress_consent` gained a nullable `hmac` TEXT column in migration 0030;
`audit_events` (#3205) ships with its `hmac` column from migration 0034.
The tag is computed over a canonical
serialization of the row's data columns — everything **except** the
`hmac` column itself — in this fixed order:

- `container_events`: `id, workspace_id, event, actor_type, actor_id,
cause, container_id, container_role, network_namespace, created_at`
- `egress_consent`: `id, workspace_id, dest_host, dest_port, pid,
process_name, decision, duration, requested_at, decided_at,
decided_by, revoked_at, revoked_by`
- `audit_events`: `id, event, actor_id, actor_email, target_type,
target_id, detail, source_ip, user_agent, created_at`

## The tag format (the contract)

Reimplementing verification only requires the standard library. The
tagged payload is built as follows:

1. Start with the table name: the literal string `container_events`,
   `egress_consent`, or `audit_events`.
2. For each covered column, in the fixed order above, append one part:
   - value is SQL `NULL` → the string `<column>=n`
   - otherwise → `s = str(value)` (Python `str()`) and the string
     `<column>=<len(s)>:<s>`, e.g. `dest_host=11:example.com` or
     `created_at=14:1695849600.123`.
3. Join the table name and all parts with a single NUL byte (`0x00`).
4. UTF-8 encode.
5. `tag = HMAC-SHA256(key, payload).hexdigest()` where `key` is the
   UTF-8 bytes of `KLANGKD_AUDIT_HMAC_KEY`.

The length prefix makes the encoding injective: no column value — not
even one containing `n`, `=`, or NUL — can impersonate another column's
NULL or splice fields. This matters because some covered values
(`dest_host`, `process_name`, `pid`) originate inside untrusted
workspaces.

Read values as their native SQLite types (`INTEGER` → `int`, `REAL` →
`float`, `TEXT` → `str`, `NULL` → `None`) and stringify with Python
`str()`; that is what the writer does.

## Recomputing offsite

Run the check against a **copy** of the database (a backup), not the
live file. The script below needs only Python ≥ 3.8:

```bash
python3 audit_hmac_check.py --db /backups/klangk.db --key "$AUDIT_HMAC_KEY"
```

```python
#!/usr/bin/env python3
"""audit_hmac_check.py — verify klangk audit-record HMACs offsite (#3174).

Exit code 0 = no mismatched tags (untagged rows are reported but do
not fail the run — decide whether untagged is acceptable for your
window); 1 = mismatched tags found (treat as tampering/corruption);
2 = usage error (argparse). Stdlib only.
"""
import argparse
import hashlib
import hmac
import sqlite3
import sys

COLUMNS = {
    "container_events": [
        "id", "workspace_id", "event", "actor_type", "actor_id",
        "cause", "container_id", "container_role",
        "network_namespace", "created_at",
    ],
    "egress_consent": [
        "id", "workspace_id", "dest_host", "dest_port", "pid",
        "process_name", "decision", "duration", "requested_at",
        "decided_at", "decided_by", "revoked_at", "revoked_by",
    ],
    "audit_events": [
        "id", "event", "actor_id", "actor_email", "target_type",
        "target_id", "detail", "source_ip", "user_agent",
        "created_at",
    ],
}


def payload(table, row, columns):
    parts = [table]
    for col in columns:
        val = row[col]
        if val is None:
            parts.append(f"{col}=n")
        else:
            sv = str(val)
            parts.append(f"{col}={len(sv)}:{sv}")
    return "\0".join(parts).encode()


def check_table(conn, table, key):
    columns = COLUMNS[table]
    names = ", ".join(columns)
    rows = conn.execute(f"SELECT {names}, hmac FROM {table}")
    ok = tagged = mismatched = untagged = 0
    first_bad = None
    for row in rows:
        d = dict(zip(columns + ["hmac"], row))
        stored = d["hmac"]
        if stored is None:
            untagged += 1  # written while tagging was disabled, or pre-migration
        elif not isinstance(stored, str):
            mismatched += 1  # a BLOB/non-text tag is itself a tampered shape
            if first_bad is None:
                first_bad = d["id"]
        elif hmac.compare_digest(
            stored, hmac.new(key, payload(table, d, columns),
                             hashlib.sha256).hexdigest()
        ):
            tagged += 1
        else:
            mismatched += 1
            if first_bad is None:
                first_bad = d["id"]
        ok += 1
    print(f"{table}: rows={ok} tagged_ok={tagged} mismatched={mismatched} "
          f"untagged={untagged}")
    if first_bad is not None:
        print(f"  first mismatch at id={first_bad}")
    return mismatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to a copy of klangk.db")
    ap.add_argument("--key", required=True,
                    help="KLANGKD_AUDIT_HMAC_KEY value in effect")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    bad = sum(check_table(conn, t, args.key.encode())
              for t in COLUMNS)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
```

Untagged rows (`hmac IS NULL`) are rows written while no key was
configured (or before the feature existed) — not evidence of tampering
by themselves; decide whether that is acceptable for your window. A
**mismatched** tag means the row's covered columns changed after
klangk wrote it: treat that as tampering or corruption and investigate
(`id` / `workspace_id` from the first mismatch is the starting point).

### FIPS

The writer routes all crypto through `hashlib`/`hmac` — the process
OpenSSL — the same boundary the [FIPS mode](../deployment/fips.md)
probe covers. The offsite checker uses the same primitives.
