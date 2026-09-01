"""Migration 0011: ``files-download`` permission (#2705).

``GET /files/download`` previously required only the ``files``
permission, so anyone who could browse/read in the viewer could also
pull raw bytes out of the container — an exfil avenue for members who
should only ever view files in-app. The route now requires the new
``files-download`` permission **in addition to** ``files`` (and ``*``
still matches both).

New grants (role-group seeding, member/group share endpoints) include
``files-download`` alongside ``files``. This migration preserves the
behavior of existing deployments: every ``Allow`` ACE with permission
``files`` gets a mirrored ``Allow`` ``files-download`` ACE for the same
principal at the same resource, inserted at the adjacent position.

Why adjacent (not appended): ACEs evaluate in position order and a
``*`` ACE matches every permission check. If a ``Deny *`` sits *after*
an ``Allow files`` entry, the ``files`` check passes but an
appended-at-the-end ``files-download`` mirror would be shadowed by the
deny — silently breaking downloads the operator never touched. The
mirror takes the very next position so it answers every check exactly
as its source ``files`` entry did.

Renumbering: ``acl_entries`` has ``UNIQUE(resource, position)`` and
SQLite enforces it per-statement (no deferring), so re-sequencing a
resource's positions can transiently collide. Existing rows are first
parked at unique negative positions, then the final sequence (with the
mirrors interleaved) is written back. The whole migration runs inside
the runner's ``BEGIN IMMEDIATE`` transaction.
"""

from klangk.model.migrations.base import Migration
from klangk.model.migrations.shared import mirror_permission_aces

_FILES = "files"
_FILES_DOWNLOAD = "files-download"


async def apply(db) -> None:
    await mirror_permission_aces(db, _FILES, _FILES_DOWNLOAD)


migration = Migration(11, "0011_files_download", apply)
