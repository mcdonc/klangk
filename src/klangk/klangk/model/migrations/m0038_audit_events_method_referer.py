"""Migration 0038: HTTP method and Referer on ``audit_events`` (#3255).

SV-222447 (rule 60) asks the audit record to carry the request's
HTTP method and ``Referer`` alongside the client IP and user agent
already recorded (#3205). Two nullable TEXT columns: rows written
before this migration read NULL for both — the same posture as
untagged HMAC rows — and so does the workstation-binding violation
row, which records only the presenting workstation pair (#3194).

The HMAC tag's column set (``_AE_HMAC_COLUMNS``) deliberately does not
grow with them: the offsite-verification contract published in
docs/reference/audit-integrity.md is fixed per column list, and
re-tagging over new columns would make every pre-#3255 row read as
tampered to a checker holding the published recipe.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE audit_events ADD COLUMN method TEXT")
    await db.execute("ALTER TABLE audit_events ADD COLUMN referer TEXT")


migration = Migration(38, "0038_audit_events_method_referer", apply)
