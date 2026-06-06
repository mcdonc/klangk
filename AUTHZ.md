# ACL Authorization System

## Context

Replace Klangk's current binary role-based authorization (admin/non-admin) with a Pyramid-style ACL system. The customer's security requirements are evolving — we don't know all the roles or resources yet, so the system needs to be general. ACLs provide the flexibility to define fine-grained permissions on a resource hierarchy without code changes.

The current system has:

- `roles` / `user_roles` tables (just "admin")
- `require_role("admin")` FastAPI dependency on admin endpoints
- Ownership checks (workspace.user_id == current_user.id)
- `workspace_access` table for binary sharing

All of these get replaced by ACLs.

## Core Concepts

- **Principals**: identity of a user, built fresh from DB on every request
  - Built-in: `system.Everyone`, `system.Authenticated`
  - User: bare UUID (e.g., `"a1b2c3d4-..."`) — matches the user's `id` column
  - Group: `group:{uuid}` (e.g., `"group:e5f6g7h8-..."`) — UUID from the `groups` table, so group renames don't break ACEs
- **Permissions**: strings naming what the user wants to do (e.g., `view`, `create`, `delete`, `manage_users`)
- **ACEs**: `(Allow/Deny, principal, permission)` attached to a resource path, ordered by position
- **Resource tree**: mirrors the API URL hierarchy — each node can have ACEs, authorization walks from the target node up to root

## Resource Tree

```text
/                              (root)
├── /workspaces                (workspace collection)
│   └── /workspaces/{id}       (specific workspace)
├── /admin
│   ├── /admin/users
│   │   └── /admin/users/{id}
│   └── /admin/invitations
└── /auth                      (public — no ACL checks)
```

Each resource path can have ACEs. Authorization walks from the matched resource up to `/`, checking each node's ACL in order. First match wins (Allow or Deny). If no match after reaching root, deny.

The resource path is derived from the **API endpoint** being called, not the frontend hash URL. The backend always knows which resource is being accessed — it's in the API path or the WebSocket message payload.

## Permissions

Permissions can grow without code changes:

| Permission           | Used on           | Replaces                                   |
| -------------------- | ----------------- | ------------------------------------------ |
| `view`               | workspaces, admin | ownership check, workspace_access          |
| `create`             | workspaces        | Authenticated default                      |
| `edit`               | workspaces        | ownership check                            |
| `delete`             | workspaces, admin | ownership check, require_role              |
| `terminal`           | workspaces        | ownership check, workspace_access          |
| `files`              | workspaces        | ownership check, workspace_access          |
| `share`              | workspaces        | ownership check                            |
| `manage_users`       | admin/users       | require_role("admin")                      |
| `manage_invitations` | admin/invitations | require_role("admin")                      |
| `admin`              | admin             | require_role("admin") — broad admin access |
| `*`                  | any               | ALL_PERMISSIONS wildcard                   |

## Database Schema

```sql
CREATE TABLE groups (
    id TEXT PRIMARY KEY,         -- UUID
    name TEXT UNIQUE NOT NULL,   -- human-readable, renamable
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE user_groups (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'manual',  -- 'manual' or 'oidc_sync' (future)
    PRIMARY KEY (user_id, group_id)
);

CREATE TABLE acl_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource TEXT NOT NULL,       -- e.g., "/", "/workspaces", "/workspaces/{actual-uuid}"
    position INTEGER NOT NULL,   -- ordering within the resource (lower = checked first)
    action TEXT NOT NULL,         -- "allow" or "deny"
    principal_type TEXT NOT NULL, -- "user", "group", or "system"
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,    -- set when principal_type = "user"
    group_id TEXT REFERENCES groups(id) ON DELETE CASCADE,  -- set when principal_type = "group"
    system_principal TEXT,        -- "Everyone" or "Authenticated" when principal_type = "system"
    permission TEXT NOT NULL,     -- "view", "create", "*", etc.
    UNIQUE(resource, position)
);
```

The `roles`, `user_roles`, and `workspace_access` tables are replaced by `groups`, `user_groups`, and `acl_entries`.

Deleting a user or group cascades — their ACEs are automatically removed.

## Default ACEs

Seeded on first startup:

```text
/                    (Allow, system:Authenticated, view)
/                    (Deny, system:Everyone, *)
/workspaces          (Allow, system:Authenticated, create)
/admin               (Allow, group:<admin-group-uuid>, *)
/admin               (Deny, system:Everyone, *)
```

The "admin" group is created at startup and seeded with the default admin user. Its UUID is used in ACEs — renaming the group later won't break anything.

Per-workspace ACEs are created dynamically:

- When a workspace is created: `(Allow, user:<owner-uuid>, *)` on `/workspaces/{id}`
- When shared: `(Allow, user:<shared-uuid>, view)`, `(Allow, user:<shared-uuid>, terminal)`, etc. on `/workspaces/{id}`
- Or: `(Allow, group:<team-uuid>, view)` for group-based sharing

## Granting Access to OIDC Users Before First Login

OIDC users don't exist in the database until they log in (JIT provisioning), so you can't create user-level ACEs for them in advance. The solution is groups: grant permissions to a group, then configure OIDC mapping rules (Phase 3) so users land in the right groups on first login. The group is the bridge between "this person will eventually show up" and "they need these permissions."

## Principal Resolution

On every request, `get_current_user` builds the principal info from the DB. Principals are matched against ACE columns:

- `system_principal = "Everyone"` matches all requests
- `system_principal = "Authenticated"` matches logged-in users
- `user_id = {uuid}` matches the specific user
- `group_id = {uuid}` matches any user who is a member of the group

No caching in the JWT — group changes take effect immediately.

## ACL Walk

Authorization checks walk from the target resource up to `/`, checking each node's ACEs in position order. First matching ACE wins (Allow or Deny). If no match after reaching root, deny.

The matching logic compares ACE columns directly against the user's principal info:

- `principal_type = "system"` → check `system_principal` against Everyone/Authenticated
- `principal_type = "user"` → check `user_id` FK against the requesting user's UUID
- `principal_type = "group"` → check `group_id` FK against the requesting user's group memberships

## API Endpoints

### ACL Management (admin)

- `GET /admin/acl/tree` — full resource tree with ACE counts per node
- `GET /admin/acl/{resource}` — list ACEs for a resource
- `GET /admin/acl/{resource}/effective` — ACEs for this resource plus inherited ACEs from parent nodes (shows the full walk)
- `PUT /admin/acl/{resource}` — replace ACEs for a resource
- `GET /admin/acl/by-principal/user/{user_id}` — all ACEs referencing this user, across all resources
- `GET /admin/acl/by-principal/group/{group_id}` — all ACEs referencing this group, across all resources

### Group Management (admin)

- `GET /admin/groups` — list all groups
- `POST /admin/groups` — create group
- `DELETE /admin/groups/{id}` — delete group (cascades ACEs)
- `PATCH /admin/groups/{id}` — rename/update group
- `GET /admin/groups/{id}/members` — list group members
- `POST /admin/groups/{id}/members` — add user to group
- `DELETE /admin/groups/{id}/members/{user_id}` — remove user from group

### User Permissions

- `GET /api/my-permissions` — returns the current user's effective permissions (for frontend UI decisions)

## Frontend

- Replace `roles.contains("admin")` checks with permission-based checks from `/api/my-permissions`
- Workspace page: ACL tab or settings section within the workspace — shows who has access and what they can do. Users with `share` permission can add/remove ACEs for users and groups on this workspace. Shows inherited ACEs from parent nodes (read-only) for context.
- Admin user list: per-user introspection — click to see all resources this user has access to and their permissions
- Admin resource tree: browse the static resource hierarchy (`/`, `/workspaces`, `/admin`, etc.), view and edit ACLs on each node. For dynamic resources (specific workspaces), navigate from the workspace list.
- Admin group management: create/delete/rename groups, add/remove members

## Phases

### Phase 1: Core ACL machinery

- Resource tree, ACL walk, principal resolution
- `groups`/`user_groups`/`acl_entries` tables
- `has_permission()` dependency replacing `require_role()`
- Migrate existing admin users to `group:admin`
- Workspace ownership/sharing via ACEs
- Default ACEs seeded on startup
- Group management API endpoints
- ACL management API endpoints
- Backend tests

### Phase 2: Frontend

- ACL editor in admin UI
- Group management in admin UI
- Permission-based UI decisions (replace role checks)
- Workspace sharing as ACL editing
- Principal introspection views

### Phase 3: OIDC-to-group mapping

- Rules-based mapping (claim contains/equals → group membership)
- Rules editable in admin UI, stored in DB
- Sync runs on every OIDC login (delete `oidc_sync` memberships, recreate from rules)
- Manual group memberships preserved
