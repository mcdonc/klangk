"""Migration 0015: workspaces.classification_banner (#2768).

A per-workspace classification marking (free text, e.g. ``UNCLASSIFIED``,
``CUI``, ``SECRET``) rendered as a persistent banner at the top and bottom
of the workspace page in the web UI and as a status line in the TUI —
markings pinned at the top and bottom of screens ("mark sensitive/classified
output when required").

``NULL`` = inherit the deploy-wide default
(``KLANGKD_CLASSIFICATION_BANNER``), which itself defaults to unset — no
marking configured anywhere renders no banner and reserves no screen space
(#2768 clarification). Resolution is at display time (not create time), so
a SIGHUP change of the deploy default re-marks every inheriting workspace
immediately.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        "ALTER TABLE workspaces ADD COLUMN classification_banner TEXT"
    )


migration = Migration(15, "0015_classification_banner", apply)
