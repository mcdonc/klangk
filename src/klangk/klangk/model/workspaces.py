"""Workspace CRUD, members, and shared-workspace listings."""

import json
import unicodedata
import uuid
from datetime import datetime, timezone

from ..netfilter import parse_allowed_domains
from .acl import ACTION_ALLOW, PRINCIPAL_GROUP, PRINCIPAL_USER
from .users import (
    AGENT_USER_ID,
    AgentPrincipalError,
    GROUP_SOURCE_WORKSPACE_ROLE,
)

# Must match the DB default and container.DEFAULT_PORTS_PER_WORKSPACE.
DEFAULT_PORTS_PER_WORKSPACE = 5

# Declarative workspace-row fields ``update_workspace`` persists (and,
# mirrored as-is, the only fields a workspace-created hook may mutate in
# place — see hooks._parse/apply; keys outside this set are provisioned,
# not declarative). Single source of truth for both callers.
UPDATABLE_WORKSPACE_FIELDS = frozenset(
    {
        "name",
        "image",
        "service_command",
        "auto_start",
        "setup_state",
        "health_check",
        "mounts",
        "env",
        "allowed_domains",
        "rejected_domains",
        "settings",
        "egress_mode",
        "per_handle_home",
        "classification_banner",
    }
)

# Per-workspace role groups created for every workspace. The key is the
# group-name suffix appended to ``<suffix>-<workspace_id>``; the value is the
# ordered list of permissions granted to that group on ``/workspaces/{id}``.
# Seeded atomically with the row in :meth:`WorkspacesModel.create_workspace_with_acl`
# so a failure mid-seed can never leave orphaned ACEs/groups (#128).
_ROLE_GROUP_PERMISSIONS: dict[str, list[str]] = {
    "owners": ["*"],
    "coders": [
        "monitor",
        "terminal",
        "egress-consent",
        "code-in-isolation",
        "exec-and-sync",
        "spectate-on-shared-terminals",
        "files",
        "files-download",
        "files-write",
    ],
    "collaborators": [
        "monitor",
        "terminal",
        "egress-consent",
        "code-in-isolation",
        "exec-and-sync",
        "code-in-shared-terminals",
        "spectate-on-shared-terminals",
        "share-terminals",
        "files",
        "files-download",
        "files-write",
    ],
    "spectators": [
        "monitor",
        "terminal",
        "spectate-on-shared-terminals",
    ],
}

# The role word for the owners group (the membership target of
# :meth:`transfer_workspace`). Kept separate from the dict above so the
# swap path reads as intent, not as a naming-convention fragment.
OWNER_ROLE = "owners"

# Upper bound on compare-and-swap retries for the settings-bag merge
# (:meth:`WorkspacesModel.update_workspace_settings`). Under normal load the
# first attempt wins; under contention we loop, re-merging on the latest blob
# each time. SQLite serializes writers, so this only loops when two PATCHes
# genuinely race — 8 is far beyond anything realistic.
_SETTINGS_CAS_RETRIES = 8

# setup_state lifecycle values (#1033). A workspace always holds
# exactly one. Descriptive, not proscriptive: created in whichever
# state matches reality.
SETUP_STATE_PENDING = "pending"
SETUP_STATE_COMPLETE = "complete"
SETUP_STATE_FAILED = "failed"
SETUP_STATES = frozenset(
    {SETUP_STATE_PENDING, SETUP_STATE_COMPLETE, SETUP_STATE_FAILED}
)

# egress_mode values (#2239). 'static' = deny + record denied attempts (no
# human prompt); 'interactive' = a pending request a human can allow/deny via
# the consent-decide client (#2310); 'allow' = default-permit -- every host is
# reachable except names in rejected_domains (NXDOMAIN'd), off-list egress is
# recorded (logged) + auto-allowed with no consent prompt (#2406). The default
# for NEW workspaces is 'interactive' so consent-gated egress is on out of the
# box; set a workspace to 'static' to opt back into silent deny + record, or
# 'allow' for permit-with-deny-list.
EGRESS_MODE_STATIC = "static"
EGRESS_MODE_INTERACTIVE = "interactive"
EGRESS_MODE_ALLOW = "allow"
EGRESS_MODE_DEFAULT = EGRESS_MODE_INTERACTIVE
EGRESS_MODES = frozenset(
    {EGRESS_MODE_STATIC, EGRESS_MODE_INTERACTIVE, EGRESS_MODE_ALLOW}
)

# Whitelisted sort columns for workspace list queries. Values are the
# real column names; the prefix (e.g. "w.") is applied by the caller.
SORT_COLUMNS = {"created": "created_at", "name": "name"}

# Classification marking bounds (#2768). The marking is free text (an
# operator-chosen label like ``UNCLASSIFIED`` / ``CUI`` / ``SECRET``),
# rendered as a one-line banner — so it must be one line, printable, and
# short enough to fit it.
CLASSIFICATION_BANNER_MAX_LEN = 120


def normalize_classification_banner(value) -> str | None:
    """Validate + normalize a classification marking (#2768).

    Accepts ``None`` (inherit the deploy default) or a one-line free-text
    label. Strips surrounding whitespace; an empty result normalizes to
    ``None`` (inherit). Rejects non-strings, values longer than
    :data:`CLASSIFICATION_BANNER_MAX_LEN`, and any control or invisible
    format character (the marking renders as a single banner line — a
    newline or tab would break the banner layout, and control chars are
    meaningless in a marking label). Invisible format characters are
    rejected too (a marking is a security label: a bidirectional override
    (U+202E) or zero-width character (U+200B) can make the banner display
    as a *different* marking than what the DB/audit log records).

    Raises ``ValueError`` with an operator-readable message.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"classification_banner must be a string, got {value!r}"
        )
    v = value.strip()
    if not v:
        return None
    if len(v) > CLASSIFICATION_BANNER_MAX_LEN:
        raise ValueError(
            "classification_banner must be at most"
            f" {CLASSIFICATION_BANNER_MAX_LEN} characters, got {len(v)}"
        )
    # Cc (control: newline/tab/DEL/NEL), Cf (format: bidi overrides,
    # zero-width chars, soft hyphen, BOM), Zl/Zp (line/paragraph
    # separators) — all either break the one-line layout or can spoof the
    # displayed marking.
    bad = {
        ch for ch in v if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"}
    }
    if bad:
        raise ValueError(
            "classification_banner must be a single line of printable"
            " text without control or invisible format characters"
            f" (found: {', '.join(f'U+{ord(ch):04X}' for ch in sorted(bad))})"
        )
    return v


def sort_order_clause(sort: str, order: str, prefix: str = "") -> str:
    """Build a deterministic ORDER BY clause for paginated workspace lists.

    ``sort`` is whitelisted against ``SORT_COLUMNS``; ``order`` is
    coerced to ASC/DESC. The ``id`` tiebreaker uses the same direction so
    offset pagination stays stable when rows share the sort key.
    """
    col = SORT_COLUMNS.get(sort, "created_at")
    p = f"{prefix}." if prefix else ""
    direction = "DESC" if order.lower() == "desc" else "ASC"
    return f"ORDER BY {p}{col} {direction}, {p}id {direction}"


# Shared workspace-row SELECT fragments + mapper (#2551): the full-row
# lookups select the same columns and build the same dict (JSON-blob
# fields decoded); one definition keeps them from drifting.
_WORKSPACE_FULL_COLUMNS = (
    "SELECT id, user_id, name, container_id, num_ports, image,"
    " service_command, auto_start, setup_state, health_check,"
    " mounts, env, allowed_domains, rejected_domains, settings,"
    " egress_mode, per_handle_home, classification_banner"
)


def _workspace_row_to_dict(row, *, auto_start=True) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "container_id": row["container_id"],
        "num_ports": row["num_ports"],
        "image": row["image"],
        "service_command": row["service_command"],
        "auto_start": bool(row["auto_start"]) if auto_start else True,
        "setup_state": row["setup_state"],
        "health_check": row["health_check"],
        "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
        "env": json.loads(row["env"]) if row["env"] else None,
        "allowed_domains": json.loads(row["allowed_domains"])
        if row["allowed_domains"]
        else None,
        "rejected_domains": json.loads(row["rejected_domains"])
        if row["rejected_domains"]
        else None,
        "settings": json.loads(row["settings"]) if row["settings"] else None,
        "egress_mode": row["egress_mode"],
        "per_handle_home": bool(row["per_handle_home"]),
        "classification_banner": row["classification_banner"],
    }


def coerce_workspace_field(key: str, value) -> object:
    """Coerce one workspace-update field to its stored representation.

    Raises ``ValueError`` on invalid enum values (checked before any
    write)."""
    if key == "setup_state":
        if value not in SETUP_STATES:
            raise ValueError(f"Invalid setup_state: {value!r}")
        return value
    if key == "egress_mode":
        if value not in EGRESS_MODES:
            raise ValueError(f"Invalid egress_mode: {value!r}")
        return value
    if key == "classification_banner":
        # Empty/whitespace text clears the override (back to
        # inheriting the deploy default); the validator raises
        # on control characters / oversize values.
        return normalize_classification_banner(value)
    if key in (
        "mounts",
        "env",
        "allowed_domains",
        "rejected_domains",
        "settings",
    ):
        return json.dumps(value) if value is not None else None
    if key in ("auto_start", "per_handle_home"):
        return 1 if value else 0
    return value


def mutate_domain_entries(current: list, spec: str, add: bool) -> bool:
    """Apply the add/remove to *current* in place; True when it was a
    no-op (entry already present for an add / absent for a remove)."""
    if add:
        if spec in (s.lower() for s in current):
            return True
        current.append(spec)
        return False
    idx = next((i for i, s in enumerate(current) if s.lower() == spec), None)
    if idx is None:
        return True
    current.pop(idx)
    return False


class WorkspacesModel:
    """Workspace CRUD/members/listings, resolved through ``app_state.db``.

    Constructed by :class:`~klangk.model.model.Model` and reached
    via ``app_state.model.workspaces``. Reaches the DB through
    ``self.app.state.db`` (the single DB instance for the whole app).

    The ``db``-param private helpers (:meth:`_insert_workspace_row` /
    :meth:`_seed_workspace_acl`) take a caller-supplied connection so they
    can run inside a larger transaction (the atomic create-with-ACL path);
    they do not reach for ``self.app.state.db`` themselves — the atomicity
    constraint (the owner ACE + role groups must commit/roll back with the
    row insert) is load-bearing (#128).
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def _insert_workspace_row(
        self,
        db,
        user_id: str,
        name: str,
        image: str | None,
        service_command: str | None,
        auto_start: bool,
        mounts: list[str] | None,
        env: dict[str, str] | None,
        setup_state: str,
        health_check: str | None,
        allowed_domains: list[str] | None = None,
        rejected_domains: list[str] | None = None,
        settings: dict | None = None,
        egress_mode: str = EGRESS_MODE_DEFAULT,
        per_handle_home: bool = True,
        classification_banner: str | None = None,
    ) -> dict:
        """INSERT a workspace row on ``db`` and return the new workspace dict.

        Runs on the caller's connection so it can participate in a larger
        transaction (see :meth:`create_workspace_with_acl`). Does not commit.
        """
        workspace_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        mounts_json = json.dumps(mounts) if mounts else None
        env_json = json.dumps(env) if env else None
        allowed_domains_json = (
            json.dumps(allowed_domains) if allowed_domains else None
        )
        rejected_domains_json = (
            json.dumps(rejected_domains) if rejected_domains else None
        )
        settings_json = json.dumps(settings) if settings else None
        await db.execute(
            "INSERT INTO workspaces"
            " (id, user_id, name, image, service_command, auto_start,"
            " setup_state, health_check, mounts, env, allowed_domains,"
            " rejected_domains, settings, egress_mode, per_handle_home,"
            " classification_banner, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                user_id,
                name,
                image,
                service_command,
                1 if auto_start else 0,
                setup_state,
                health_check,
                mounts_json,
                env_json,
                allowed_domains_json,
                rejected_domains_json,
                settings_json,
                egress_mode,
                1 if per_handle_home else 0,
                classification_banner,
                created_at,
            ),
        )
        return {
            "id": workspace_id,
            "user_id": user_id,
            "name": name,
            "image": image,
            "service_command": service_command,
            "auto_start": auto_start,
            "setup_state": setup_state,
            "health_check": health_check,
            "mounts": mounts,
            "env": env,
            "allowed_domains": allowed_domains,
            "rejected_domains": rejected_domains,
            "settings": settings,
            "egress_mode": egress_mode,
            "per_handle_home": per_handle_home,
            "classification_banner": classification_banner,
            "num_ports": DEFAULT_PORTS_PER_WORKSPACE,
            "created_at": created_at,
        }

    async def get_workspace_role_groups(
        self, db, workspace_id: str
    ) -> list[dict]:
        """The workspace's seeded role groups, by marker + workspace id.

        Runs on the caller's connection (both callers — teardown and
        ownership transfer — are inside transactions). Matching is
        ``source = 'workspace-role'`` plus the workspace-id UUID suffix
        of the name (#2750): the UUID is unique per workspace, so no
        role-suffix list is duplicated here. Returns ``[{id, name}]``.
        """
        cursor = await db.execute(
            "SELECT id, name FROM groups WHERE source = ? AND name LIKE ?",
            (GROUP_SOURCE_WORKSPACE_ROLE, f"%-{workspace_id}"),
        )
        return [
            {"id": row["id"], "name": row["name"]}
            for row in await cursor.fetchall()
        ]

    async def _seed_workspace_acl(self, db, ws: dict, user_id: str) -> None:
        """Seed the owner ACE and per-workspace role groups on ``db``.

        Writes the owner ``Allow`` ACE at position 0, then creates the four
        role groups (``owners``/``coders``/``collaborators``/``spectators``)
        with their permission ACEs at incrementing positions, and adds the
        creator to the ``owners`` group. Role groups are inserted with
        ``source = 'workspace-role'`` so global group lists can hide them
        (#2750). Runs on the caller's connection so it commits/rolls back
        with the surrounding transaction. Must stay in sync with
        :meth:`delete_workspace`'s teardown.
        """
        resource = f"/workspaces/{ws['id']}"
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource,
                0,
                ACTION_ALLOW,
                PRINCIPAL_USER,
                user_id,
                None,
                None,
                "*",
            ),
        )
        pos = 1
        for suffix, perms in _ROLE_GROUP_PERMISSIONS.items():
            group_name = f"{suffix}-{ws['id']}"
            group_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO groups (id, name, description, source)"
                " VALUES (?, ?, ?, ?)",
                (
                    group_id,
                    group_name,
                    f"Workspace role group: {suffix} of workspace"
                    f" {ws['name']}",
                    GROUP_SOURCE_WORKSPACE_ROLE,
                ),
            )
            for perm in perms:
                await db.execute(
                    "INSERT INTO acl_entries"
                    " (resource, position, action, principal_type,"
                    " user_id, group_id, system_principal, permission)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource,
                        pos,
                        ACTION_ALLOW,
                        PRINCIPAL_GROUP,
                        None,
                        group_id,
                        None,
                        perm,
                    ),
                )
                pos += 1
            if suffix == "owners":
                await db.execute(
                    "INSERT OR IGNORE INTO user_groups"
                    " (user_id, group_id, source) VALUES (?, ?, ?)",
                    (user_id, group_id, "manual"),
                )

    async def create_workspace_with_acl(
        self,
        user_id: str,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str = SETUP_STATE_COMPLETE,
        health_check: str | None = None,
        allowed_domains: list[str] | None = None,
        rejected_domains: list[str] | None = None,
        settings: dict | None = None,
        egress_mode: str = EGRESS_MODE_DEFAULT,
        per_handle_home: bool = True,
        classification_banner: str | None = None,
    ) -> dict:
        """Create a workspace row AND seed its owner ACE + role groups.

        The row insert and the ACL/group seeding run in a **single
        transaction**, so any failure rolls the whole thing back — no
        orphaned row, ACEs, or role groups (#128). Directory creation and
        port allocation happen later in the service layer
        (:func:`workspaces.create_workspace`); port-allocation failure is
        cleaned up by :meth:`delete_workspace`, which removes everything
        this function wrote.
        """
        if user_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "The system agent cannot own a workspace (seeding it a"
                " wildcard owner ACE + owners-group membership makes its"
                " UUID a privileged principal) — system agent"
            )
        if setup_state not in SETUP_STATES:
            raise ValueError(f"Invalid setup_state: {setup_state!r}")
        if egress_mode not in EGRESS_MODES:
            raise ValueError(f"Invalid egress_mode: {egress_mode!r}")
        if not isinstance(per_handle_home, bool):
            raise ValueError(f"Invalid per_handle_home: {per_handle_home!r}")
        classification_banner = normalize_classification_banner(
            classification_banner
        )
        async with self.app.state.db.transaction() as db:
            ws = await self._insert_workspace_row(
                db,
                user_id,
                name,
                image=image,
                service_command=service_command,
                auto_start=auto_start,
                mounts=mounts,
                env=env,
                setup_state=setup_state,
                health_check=health_check,
                allowed_domains=allowed_domains,
                rejected_domains=rejected_domains,
                settings=settings,
                egress_mode=egress_mode,
                per_handle_home=per_handle_home,
                classification_banner=classification_banner,
            )
            await self._seed_workspace_acl(db, ws, user_id)
            return ws

    async def create_workspace(
        self,
        user_id: str,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str = SETUP_STATE_COMPLETE,
        health_check: str | None = None,
        allowed_domains: list[str] | None = None,
        rejected_domains: list[str] | None = None,
        settings: dict | None = None,
        egress_mode: str = EGRESS_MODE_DEFAULT,
        per_handle_home: bool = True,
        classification_banner: str | None = None,
    ) -> dict:
        """Insert a workspace row only (no ACL seeding).

        Prefer :meth:`create_workspace_with_acl` for normal workspace
        creation — it seeds the owner ACE and role groups atomically and is
        what the service layer uses. This row-only primitive is kept for
        callers that manage ACLs separately.
        """
        if setup_state not in SETUP_STATES:
            raise ValueError(f"Invalid setup_state: {setup_state!r}")
        if egress_mode not in EGRESS_MODES:
            raise ValueError(f"Invalid egress_mode: {egress_mode!r}")
        if not isinstance(per_handle_home, bool):
            raise ValueError(f"Invalid per_handle_home: {per_handle_home!r}")
        classification_banner = normalize_classification_banner(
            classification_banner
        )
        async with self.app.state.db.transaction() as db:
            return await self._insert_workspace_row(
                db,
                user_id,
                name,
                image=image,
                service_command=service_command,
                auto_start=auto_start,
                mounts=mounts,
                env=env,
                setup_state=setup_state,
                health_check=health_check,
                allowed_domains=allowed_domains,
                rejected_domains=rejected_domains,
                settings=settings,
                egress_mode=egress_mode,
                per_handle_home=per_handle_home,
                classification_banner=classification_banner,
            )

    async def list_workspaces(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List a page of workspaces owned by ``user_id``.

        Returns a pagination envelope:
        ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
        ``sort`` (``created``/``name``) and ``order`` (``asc``/``desc``) are
        whitelisted; ``q`` filters by name substring. The ``id`` tiebreaker
        keeps offset pagination deterministic.
        """
        order_by = sort_order_clause(sort, order)
        where = "WHERE user_id = ?"
        params: list = [user_id]
        if q:
            where += " AND name LIKE '%' || ? || '%'"
            params.append(q)
        params.extend([limit + 1, offset])
        rows = await self.app.state.db.fetchall(
            "SELECT id, name, container_id, image, service_command,"
            " auto_start, setup_state, health_check, mounts, env,"
            " allowed_domains, rejected_domains, settings, egress_mode,"
            " per_handle_home, classification_banner, created_at"
            " FROM workspaces"
            f" {where} {order_by} LIMIT ? OFFSET ?",
            tuple(params),
        )
        items = [
            {
                "id": row["id"],
                "name": row["name"],
                "container_id": row["container_id"],
                "image": row["image"],
                "service_command": row["service_command"],
                "auto_start": bool(row["auto_start"]),
                "setup_state": row["setup_state"],
                "health_check": row["health_check"],
                "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
                "env": json.loads(row["env"]) if row["env"] else None,
                "allowed_domains": json.loads(row["allowed_domains"])
                if row["allowed_domains"]
                else None,
                "rejected_domains": json.loads(row["rejected_domains"])
                if row["rejected_domains"]
                else None,
                "settings": json.loads(row["settings"])
                if row["settings"]
                else None,
                "egress_mode": row["egress_mode"],
                "per_handle_home": bool(row["per_handle_home"]),
                "classification_banner": row["classification_banner"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        has_more = len(items) > limit
        items = items[:limit]
        return {
            "items": items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }

    async def _shared_workspace_rows(
        self,
        user_id: str,
        sort: str,
        order: str,
        q: str | None,
        limit: int,
        offset: int,
    ) -> list:
        """Rows for workspaces shared with (not owned by) *user_id*, via
        a direct user-level ACE or a group-level ACE on
        ``/workspaces/{id}``."""
        order_by = sort_order_clause(sort, order, prefix="w")
        name_filter = " AND w.name LIKE '%' || ? || '%'" if q else ""
        # The group-ids read runs on its own connection (it did before
        # the fetchall conversion too), so it never shared the page
        # read's snapshot.
        group_ids = await self.app.state.model.users.get_user_group_ids(
            user_id
        )
        group_placeholders = ",".join("?" for _ in group_ids)
        group_clause = (
            f" OR (ae.principal_type = {PRINCIPAL_GROUP}"
            f" AND ae.group_id IN ({group_placeholders}))"
            if group_ids
            else ""
        )
        rows = await self.app.state.db.fetchall(
            "SELECT DISTINCT w.id, w.name, w.container_id, w.image,"
            " w.service_command, w.auto_start, w.setup_state,"
            " w.health_check, w.mounts, w.env, w.allowed_domains, w.rejected_domains,"
            " w.settings, w.egress_mode, w.per_handle_home,"
            " w.classification_banner, w.created_at,"
            " u.email AS owner_email"
            " FROM workspaces w"
            " JOIN acl_entries ae ON ae.resource = '/workspaces/' || w.id"
            " JOIN users u ON w.user_id = u.id"
            " WHERE ae.action = ? AND w.user_id != ?"
            "   AND ("
            f"    (ae.principal_type = {PRINCIPAL_USER} AND ae.user_id = ?)"
            f"    {group_clause}"
            "   )"
            f"{name_filter}"
            f" {order_by} LIMIT ? OFFSET ?",
            (
                ACTION_ALLOW,
                user_id,
                user_id,
                *group_ids,
                *([q] if q else []),
                limit + 1,
                offset,
            ),
        )
        return rows

    @staticmethod
    def _shared_workspace_item(row) -> dict:
        """One shared-workspaces row as an API item (JSON columns
        parsed)."""
        return {
            "id": row["id"],
            "name": row["name"],
            "container_id": row["container_id"],
            "image": row["image"],
            "service_command": row["service_command"],
            "auto_start": bool(row["auto_start"]),
            "setup_state": row["setup_state"],
            "health_check": row["health_check"],
            "mounts": json.loads(row["mounts"]) if row["mounts"] else None,
            "env": json.loads(row["env"]) if row["env"] else None,
            "allowed_domains": json.loads(row["allowed_domains"])
            if row["allowed_domains"]
            else None,
            "rejected_domains": json.loads(row["rejected_domains"])
            if row["rejected_domains"]
            else None,
            "settings": json.loads(row["settings"])
            if row["settings"]
            else None,
            "egress_mode": row["egress_mode"],
            "per_handle_home": bool(row["per_handle_home"]),
            "classification_banner": row["classification_banner"],
            "created_at": row["created_at"],
            "owner_email": row["owner_email"],
        }

    async def list_shared_workspaces(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        """List a page of workspaces shared with (not owned by) this user.

        Access is granted through either a direct user-level ACE or a
        group-level ACE on ``/workspaces/{id}``. Returns a pagination
        envelope: ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
        ``sort``/``order``/``q`` as in :meth:`list_workspaces`.
        """
        rows = await self._shared_workspace_rows(
            user_id, sort, order, q, limit, offset
        )
        items = [self._shared_workspace_item(row) for row in rows]
        has_more = len(items) > limit
        items = items[:limit]
        return {
            "items": items,
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
        }

    async def get_workspace(
        self, workspace_id: str, user_id: str | None = None
    ) -> dict | None:
        """Get a workspace by ID.

        If user_id is provided, restricts to workspaces owned by that user.
        Access control for shared workspaces is handled by the ACL layer.
        """
        async with self.app.state.db.transaction() as db:
            if user_id is not None:
                cursor = await db.execute(
                    _WORKSPACE_FULL_COLUMNS
                    + " FROM workspaces WHERE id = ? AND user_id = ?",
                    (workspace_id, user_id),
                )
            else:
                cursor = await db.execute(
                    _WORKSPACE_FULL_COLUMNS + " FROM workspaces WHERE id = ?",
                    (workspace_id,),
                )
            row = await cursor.fetchone()
            if row is None:
                return None
            return _workspace_row_to_dict(row)

    async def get_workspace_by_id(self, workspace_id: str) -> dict | None:
        """Get a workspace by ID without access control (for admin use)."""
        row = await self.app.state.db.fetchone(
            _WORKSPACE_FULL_COLUMNS + " FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        if row is None:
            return None
        return _workspace_row_to_dict(row)

    async def existing_workspace_ids(self) -> set[str]:
        """Return the IDs of every workspace row (for orphan-file sweeps).

        Used by the periodic sidecar-token sweep (:meth:`ContainerRegistry.
        _sweep_orphaned_sidecar_tokens`) to tell token files whose workspace
        still exists from orphans left by a deleted/crashed workspace (#2309).
        """
        rows = await self.app.state.db.fetchall("SELECT id FROM workspaces")
        return {row["id"] for row in rows}

    async def get_workspace_members(self, workspace_id: str) -> list[dict]:
        """Get users who have been granted access to a workspace via ACL.

        Returns users with direct user-level ACEs on /workspaces/{id},
        excluding the workspace owner.
        """
        rows = await self.app.state.db.fetchall(
            "SELECT DISTINCT u.id, u.email, u.handle FROM users u"
            " JOIN acl_entries ae ON ae.user_id = u.id"
            " JOIN workspaces w ON w.id = ?"
            " WHERE ae.resource = ? AND ae.principal_type = ?"
            "   AND ae.action = ? AND u.id != w.user_id"
            " ORDER BY u.email",
            (
                workspace_id,
                f"/workspaces/{workspace_id}",
                PRINCIPAL_USER,
                ACTION_ALLOW,
            ),
        )
        return [
            {
                "id": row["id"],
                "email": row["email"],
                "handle": row["handle"],
            }
            for row in rows
        ]

    async def delete_workspace(self, workspace_id: str, user_id: str) -> bool:
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM workspaces WHERE id = ? AND user_id = ?",
                (workspace_id, user_id),
            )
            if cursor.rowcount == 0:
                return False
            # Clean up ACL entries for this workspace
            resource = f"/workspaces/{workspace_id}"
            await db.execute(
                "DELETE FROM acl_entries WHERE resource = ?", (resource,)
            )
            # Clean up per-workspace role groups and their memberships.
            # Found by the source marker + workspace id (#2750) — no
            # role-suffix list, so any group seeded for this workspace
            # tears down even with a suffix outside the current four.
            for group in await self.get_workspace_role_groups(
                db, workspace_id
            ):
                group_id = group["id"]
                await db.execute(
                    "DELETE FROM user_groups WHERE group_id = ?",
                    (group_id,),
                )
                await db.execute(
                    "DELETE FROM acl_entries WHERE group_id = ?",
                    (group_id,),
                )
                await db.execute(
                    "DELETE FROM groups WHERE id = ?", (group_id,)
                )
            # Clean up port allocations
            await db.execute(
                "DELETE FROM port_allocations WHERE workspace_id = ?",
                (workspace_id,),
            )
            return True

    async def update_workspace_container(
        self, workspace_id: str, container_id: str | None
    ) -> None:
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "UPDATE workspaces SET container_id = ? WHERE id = ?",
                (container_id, workspace_id),
            )

    async def update_workspace_settings(
        self,
        workspace_id: str,
        user_id: str,
        patch: dict,
    ) -> dict | None:
        """Partial-merge update of the ``settings`` bag (#864).

        Compare-and-swap on the raw settings blob: load it, merge *patch* in
        Python (each key set/replace, ``None`` value deletes that key), and
        UPDATE only if the blob is unchanged since the read
        (``settings IS ?`` is NULL-safe, unlike ``=``). If another writer
        committed in between, ``rowcount`` is 0 and the loop re-reads the
        latest blob and re-applies the patch on that base — so two
        concurrent PATCHes to *different* keys can't clobber each other (the
        classic read-modify-write lost-update).

        Returns the post-merge settings dict (or ``None`` if the bag is now
        empty), or ``None`` if the workspace wasn't found / isn't owned by
        *user_id*.

        *patch* must already be validated + normalized by
        :func:`klangk.workspace_settings.validate_settings_patch` — this
        method trusts the keys are known and the non-null values are
        coerced. It owns only the merge + persistence + the empty-bag →
        NULL mapping.
        """
        for _ in range(_SETTINGS_CAS_RETRIES):
            async with self.app.state.db.transaction() as db:
                cursor = await db.execute(
                    "SELECT settings FROM workspaces"
                    " WHERE id = ? AND user_id = ?",
                    (workspace_id, user_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                old_blob = row["settings"]
                current = json.loads(old_blob) if old_blob else {}
                for key, value in patch.items():
                    if value is None:
                        current.pop(key, None)
                    else:
                        current[key] = value
                new_blob = json.dumps(current) if current else None
                cursor = await db.execute(
                    "UPDATE workspaces SET settings = ?"
                    " WHERE id = ? AND user_id = ? AND settings IS ?",
                    (new_blob, workspace_id, user_id, old_blob),
                )
                if cursor.rowcount == 1:
                    return current if current else None
                # rowcount 0 — settings changed under us (or the row went
                # away); loop re-reads the latest blob and re-applies.
        raise RuntimeError(  # pragma: no cover - only under extreme contention
            f"settings CAS retry exhausted for workspace {workspace_id}"
        )

    async def update_workspace(
        self,
        workspace_id: str,
        user_id: str,
        **fields: str | None,
    ) -> bool:
        """Update workspace fields. Only provided fields are changed."""
        to_set = {
            k: coerce_workspace_field(k, v)
            for k, v in fields.items()
            if k in UPDATABLE_WORKSPACE_FIELDS
        }
        if not to_set:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in to_set)
        values = list(to_set.values()) + [workspace_id, user_id]
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                f"UPDATE workspaces SET {set_clause}"  # noqa: S608
                " WHERE id = ? AND user_id = ?",
                values,
            )
            return cursor.rowcount > 0

    async def get_consent_pause(self, workspace_id: str) -> float | None:
        """The epoch second consent prompting is paused until, or None (#2332).

        Returns the raw stored value WITHOUT an expiry check; the caller
        compares it against ``now`` (a value in the past means the pause has
        elapsed and the workspace is effectively not paused). None means not
        paused, or the workspace does not exist.
        """
        row = await self.app.state.db.fetchone(
            "SELECT consent_paused_until FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        if row is None:
            return None
        val = row["consent_paused_until"]
        return float(val) if isinstance(val, (int, float)) else None

    async def set_consent_pause(
        self, workspace_id: str, until: float | None
    ) -> bool:
        """Set (or clear, when ``until`` is None) the consent-pause window (#2332).

        ``until`` is an epoch second (``now + window``), or None to clear an
        active pause. Returns True if the workspace exists (a row was
        updated), else False. No expiry math here: a past ``until`` is a valid
        store (read as "not paused" by :meth:`get_consent_pause`); the caller
        computes the window.
        """
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE workspaces SET consent_paused_until = ? WHERE id = ?",
                (until, workspace_id),
            )
            return cursor.rowcount > 0

    async def _mutate_domain_list(
        self, workspace_id: str, column: str, *, add: bool, entry: str
    ) -> bool:
        """Append/remove *entry* on a workspace's domain-list column (#2552).

        The shared body of the add/remove allowed/rejected quartet
        (#2368/#2369/#2370): normalize via :func:`parse_allowed_domains`,
        then compare-and-swap on the JSON blob (like
        :meth:`update_workspace_settings`) so two concurrent mutations
        can't lose one. Case-insensitive de-dup (add) / match (remove).
        Returns True if the workspace exists and *entry* is in the wanted
        state afterwards (idempotent); False if the workspace is missing
        or *entry* is malformed.
        """
        try:
            normalized = parse_allowed_domains([entry])
        except ValueError:
            return False
        if not normalized:
            return False
        spec = normalized[0].lower()
        for _ in range(_SETTINGS_CAS_RETRIES):
            async with self.app.state.db.transaction() as db:
                cursor = await db.execute(
                    f"SELECT {column} FROM workspaces WHERE id = ?",
                    (workspace_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return False
                old_blob = row[column]
                current = json.loads(old_blob) if old_blob else []
                if mutate_domain_entries(current, spec, add):
                    return True  # already present/absent (idempotent)
                new_blob = json.dumps(current)
                cursor = await db.execute(
                    f"UPDATE workspaces SET {column} = ?"
                    f" WHERE id = ? AND {column} IS ?",
                    (new_blob, workspace_id, old_blob),
                )
                if cursor.rowcount == 1:
                    return True
        return False  # pragma: no cover - CAS exhausted under contention

    async def add_allowed_domain(self, workspace_id: str, entry: str) -> bool:
        """Append ``entry`` (``host[:port]``) to a workspace's
        ``allowed_domains`` (#2368).

        A ``forever`` egress-consent allow persists by mutating the workspace's
        allow-list, which the network sidecar re-reads on (re)start -- so the
        allow survives a container/sidecar restart (the deciding connection
        already got its in-memory ACCEPT from the verdict). Unlike
        :meth:`update_workspace` this is a server-internal mutation with no
        owner-user gate: the consent verdict already authorized it.

        Compare-and-swap on the JSON blob (like
        :meth:`update_workspace_settings`) so two concurrent appends can't lose
        one. Normalizes ``entry`` to lowercase and de-duplicates
        case-insensitively. Returns True if the workspace exists and ``entry``
        is in the list afterwards (added or already present); False if the
        workspace is missing or ``entry`` is malformed (the caller -- the
        verdict path -- must not break on a persistence failure).
        """
        return await self._mutate_domain_list(
            workspace_id, "allowed_domains", add=True, entry=entry
        )

    async def add_rejected_domain(self, workspace_id: str, entry: str) -> bool:
        """Append ``entry`` (``host[:port]``) to a workspace's
        ``rejected_domains`` (#2369).

        The deny counterpart of :meth:`add_allowed_domain`: a ``forever``
        egress-consent deny persists by mutating the workspace's deny-list,
        which the network sidecar re-reads on (re)start and NXDOMAINs
        unconditionally -- so the deny survives a container/sidecar restart
        (the deciding connection already got its in-memory REJECT from the
        verdict). Like :meth:`add_allowed_domain` this is a server-internal
        mutation with no owner-user gate, compare-and-swap on the JSON blob,
        lowercased + de-duplicated case-insensitively. Returns True if the
        workspace exists and ``entry`` is in the list afterwards; False if the
        workspace is missing or ``entry`` is malformed.

        Note: the sidecar's reject enforcement is name-level (a rejected name
        is NXDOMAIN'd before resolution, regardless of port), so a
        ``host:port`` entry blocks the whole name; the port is retained for
        symmetry with :meth:`add_allowed_domain` and the audit row, not for
        scoping.
        """
        return await self._mutate_domain_list(
            workspace_id, "rejected_domains", add=True, entry=entry
        )

    async def remove_allowed_domain(
        self, workspace_id: str, entry: str
    ) -> bool:
        """Remove ``entry`` (``host[:port]``) from a workspace's
        ``allowed_domains`` (#2370) -- the inverse of :meth:`add_allowed_domain`.

        Revoking a ``forever`` egress-consent allow retracts the durable entry
        it added (so the allow does not re-apply on the next sidecar restart);
        the in-memory ACCEPT rules are dropped separately by the sidecar.
        Compare-and-swap on the JSON blob, case-insensitive match, mirroring
        :meth:`add_allowed_domain`. Returns True if the workspace exists and
        ``entry`` is absent from the list afterwards (removed or already
        absent -- idempotent); False if the workspace is missing or ``entry``
        is malformed.
        """
        return await self._mutate_domain_list(
            workspace_id, "allowed_domains", add=False, entry=entry
        )

    async def remove_rejected_domain(
        self, workspace_id: str, entry: str
    ) -> bool:
        """Remove ``entry`` from a workspace's ``rejected_domains`` (#2370) --
        the inverse of :meth:`add_rejected_domain`.

        Revoking a ``forever`` egress-consent deny retracts the durable entry
        it added (so the deny does not re-apply on the next sidecar restart);
        the in-memory REJECT rules are dropped separately by the sidecar.
        Compare-and-swap on the JSON blob, case-insensitive match, mirroring
        :meth:`add_rejected_domain`. Returns True if the workspace exists and
        ``entry`` is absent from the list afterwards (removed or already
        absent -- idempotent); False if the workspace is missing or ``entry``
        is malformed.
        """
        return await self._mutate_domain_list(
            workspace_id, "rejected_domains", add=False, entry=entry
        )

    async def transfer_workspace(
        self,
        workspace_id: str,
        new_owner_id: str,
    ) -> dict | None:
        """Transfer workspace ownership to a different user.

        Updates the workspace ``user_id``, the owner ACE (position 0), and
        the ``owners`` role group membership atomically.  Returns the
        updated workspace dict, or ``None`` if the workspace does not exist.

        Raises ``ValueError`` if the new owner already owns a workspace
        with the same name (violating the UNIQUE constraint) or if the
        target is the system agent.
        """
        if new_owner_id == AGENT_USER_ID:
            raise AgentPrincipalError(
                "Cannot transfer a workspace to the system agent"
            )

        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT id, user_id, name FROM workspaces WHERE id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            old_owner_id = row["user_id"]
            ws_name = row["name"]

            if old_owner_id == new_owner_id:
                raise ValueError("Target user is already the owner")

            # Check UNIQUE(user_id, name) won't be violated.
            dup = await db.execute(
                "SELECT 1 FROM workspaces"
                " WHERE user_id = ? AND name = ? AND id != ?",
                (new_owner_id, ws_name, workspace_id),
            )
            if await dup.fetchone():
                raise ValueError(
                    f"Target user already owns a workspace named {ws_name!r}"
                )

            # 1. Update workspace owner.
            await db.execute(
                "UPDATE workspaces SET user_id = ? WHERE id = ?",
                (new_owner_id, workspace_id),
            )

            # 2. Update the owner ACE (position 0) to point at the new owner.
            resource = f"/workspaces/{workspace_id}"
            await db.execute(
                "UPDATE acl_entries SET user_id = ?"
                " WHERE resource = ? AND position = 0"
                " AND principal_type = ?",
                (new_owner_id, resource, PRINCIPAL_USER),
            )

            # 3. Swap owners-group membership: remove old owner, add new.
            # The owners group is found by the source marker + workspace
            # id (#2750), not by reconstructed name.
            owners_group = next(
                (
                    g
                    for g in await self.get_workspace_role_groups(
                        db, workspace_id
                    )
                    if g["name"].startswith(f"{OWNER_ROLE}-")
                ),
                None,
            )
            if owners_group:
                group_id = owners_group["id"]
                await db.execute(
                    "DELETE FROM user_groups WHERE user_id = ? AND group_id = ?",
                    (old_owner_id, group_id),
                )
                await db.execute(
                    "INSERT OR IGNORE INTO user_groups"
                    " (user_id, group_id, source) VALUES (?, ?, ?)",
                    (new_owner_id, group_id, "manual"),
                )

        return await self.get_workspace_by_id(workspace_id)

    async def get_user_workspaces_with_containers(
        self, user_id: str
    ) -> list[dict]:
        rows = await self.app.state.db.fetchall(
            "SELECT id, container_id FROM workspaces WHERE user_id = ? AND container_id IS NOT NULL",
            (user_id,),
        )
        return [
            {"id": row["id"], "container_id": row["container_id"]}
            for row in rows
        ]

    async def get_user_workspace_ids(self, user_id: str) -> list[str]:
        """Every workspace id owned by *user_id*, started or not (#2525).

        The admission quota counts running-or-mid-start workspaces over
        ALL of the owner's rows: a fresh workspace's ``container_id`` is
        only persisted after ``podman create`` (seconds after admission),
        so the ``container_id IS NOT NULL`` prefilter of
        :meth:`get_user_workspaces_with_containers` cannot see a sibling
        start that is in flight — the exact race the gate exists to
        close. Runtime state (running / lock-held) is joined in by the
        caller against the in-memory registry, not the DB.
        """
        rows = await self.app.state.db.fetchall(
            "SELECT id FROM workspaces WHERE user_id = ?",
            (user_id,),
        )
        return [row["id"] for row in rows]

    async def list_auto_start_workspaces(self) -> list[dict]:
        """List all workspaces with auto_start enabled."""
        rows = await self.app.state.db.fetchall(
            _WORKSPACE_FULL_COLUMNS + " FROM workspaces WHERE auto_start = 1",
        )
        # auto_start is pinned True by the WHERE clause; the mapper's
        # bool() would also be True, but pass the explicit form so the
        # query's intent stays readable at the call site.
        return [_workspace_row_to_dict(row, auto_start=True) for row in rows]
