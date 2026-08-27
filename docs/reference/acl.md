# ACL System

Klangk uses an Access Control List (ACL) system to manage permissions. Instead of simple admin/non-admin roles, permissions are defined as ACL entries (ACEs) attached to resources in a tree hierarchy. This allows fine-grained control over who can do what, without code changes.

## Core Concepts

- **Resources**: paths in a tree that mirror the URL structure (`/`, `/workspaces`, `/workspaces/{id}`, `/admin`, `/admin/users`, etc.)
- **Principals**: who the ACE applies to — a specific user, a group, or a system principal (`Everyone` or `Authenticated`)
- **Permissions**: what action is allowed or denied (e.g., `view`, `create`, `edit`, `delete`, `terminal`, `files`, `share`, `*`)
- **ACEs**: `(Allow/Deny, principal, permission)` entries ordered by position on a resource
- **ACL walk**: when checking permission, the system walks from the target resource up to `/`, checking each node's ACEs in order. First match wins. If no match after reaching root, access is denied.

## Resource Tree

```text
/                              (root)
├── /workspaces                (workspace collection)
│   └── /workspaces/{id}       (specific workspace)
├── /admin
│   ├── /admin/users
│   ├── /admin/invitations
│   └── /admin/groups
└── /auth                      (public — no ACL checks)
```

## Default ACEs (seeded on first startup)

| Resource      | Action | Principal     | Permission |
| ------------- | ------ | ------------- | ---------- |
| `/`           | Allow  | Authenticated | `view`     |
| `/`           | Deny   | Everyone      | `*`        |
| `/workspaces` | Allow  | group:admin   | `create`   |
| `/groups`     | Allow  | group:admin   | `create`   |
| `/admin`      | Allow  | group:admin   | `*`        |
| `/admin`      | Deny   | Everyone      | `*`        |

These defaults mean: any logged-in user can view pages; only members of the `admin` group can create workspaces, create groups, or access admin functions; unauthenticated users are denied everything.

### Granting workspace creation to non-admin users

By default only administrators can create workspaces (#2569). To allow
other users or groups to create workspaces, add an ACL entry on the
`/workspaces` collection resource via the web UI:

1. Navigate to **Admin → ACL** (or the Advanced ACL editor).
2. Select the `/workspaces` resource.
3. Add an **Allow** entry for the `create` permission, targeting either:
   - A specific **group** (e.g., a "developers" group you've created) — all
     members of that group can then create workspaces.
   - The **Authenticated** system principal — restores the pre-#2569
     behavior where any logged-in user can create workspaces.
4. Ensure the new entry's position is lower than any Deny entry on the
   same resource (lower position = checked first).

The create button in the web UI automatically appears/disappears based
on the user's effective `create` permission on `/workspaces`.

## Groups

Groups replace the old role system. A group is a named collection of users. Two built-in groups are created automatically on first startup:

- **`admin`** — the default admin user is added to it; members can create workspaces and access admin functions.
- **`members`** — every new user (registration, invitation, OIDC first login, admin-created) is added automatically. Has no permissions by default, but deployers can grant `create` on `/workspaces` to this group to let all members create workspaces.

**Admin UI**: Admin > Groups tab — create/delete groups, add/remove members.

**API endpoints**:

- `GET /api/v1/admin/groups` — list all groups
- `POST /api/v1/admin/groups` — create group `{"name": "...", "description": "..."}`
- `DELETE /api/v1/admin/groups/{id}` — delete group (cascades: removes all ACEs referencing it)
- `POST /api/v1/admin/groups/{id}/members` — add user `{"user_id": "..."}`
- `DELETE /api/v1/admin/groups/{id}/members/{user_id}` — remove user

### Per-workspace role groups (#2750)

Every workspace also seeds four **role groups** — `owners-<workspace_id>`,
`coders-<workspace_id>`, `collaborators-<workspace_id>`, and
`spectators-<workspace_id>` — that carry its sharing roles. They live in
the same `groups` table but are marked with `source = "workspace-role"`
(rows for human-managed groups carry `source = "manual"`). Both list
endpoints accept a `source` query filter, so group pickers can ask for
`?source=manual` and hide the machine-generated names; per-workspace
roles remain available via `GET /api/v1/workspaces/{id}/roles`.

A role group is grantable **only on its own workspace's resource**:
APIs that would grant workspace B's role group on workspace A (or on any
other resource) are rejected with a 400 error.

## Workspace Permissions

When a workspace is created, the owner gets a `(Allow, user:{id}, *)` ACE on `/workspaces/{id}`. This grants full access: view, edit, delete, share, terminal, files, export.

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) grants `view`, `terminal`, `files`, `files-download`, and `files-write`. For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs.

**Permissions checked on workspace resources**:

| Permission       | Controls                                                          |
| ---------------- | ----------------------------------------------------------------- |
| `view`           | Can see the workspace exists                                      |
| `terminal`       | Can open a terminal / exec commands                               |
| `files`          | Can browse/read files                                             |
| `files-download` | Can download raw bytes via `/files/download` (needs `files` too)  |
| `files-write`    | Can mutate files: upload, rename, delete (needs `files` too)      |
| `exec-and-sync`  | Can run one-shot commands (`klangk exec`) and sync (`klangk sync`) against the workspace |
| `edit`           | Can change workspace settings (name, image, command, mounts, env) |
| `share`          | Can manage who has access (Sharing tab)                           |
| `delete`         | Can delete the workspace                                          |
| `export`         | Can export the workspace as a `.tar.gz` archive (#2707)           |
| `*`              | All of the above                                                  |

Withholding `files-download` keeps the in-app file viewer working for text
files (read via `/files/content`) but hides every download affordance and
returns 403 from `/files/download`. Binary renderers (image, PDF, video,
spreadsheet) fetch raw bytes through the download endpoint, so they cannot
render without it.

Withholding `files-write` disables every mutating route — upload
(`/files/upload`), rename (`/files/rename`), and delete (`DELETE
/files`) — so a `files`-only member can browse and read but not modify
the workspace. In the file viewer the matching affordances disappear: no
drag-and-drop zone or upload hints, no Rename/Delete in the context menu,
and text-editor renderers go read-only (their Save uploads through that
endpoint).

Note the limit of the download gate: it blocks byte-perfect, unbounded export
(any file size, whole directories as tar.gz), **not** content access.
`files` alone still lets a member read any file up to 1 MB via
`/files/content` — text files intact, binary files mostly (the bytes are
decoded lossily). To cut a member off from workspace content entirely,
withhold `files` as well.

`export` is a workspace permission checked on `/workspaces/{id}`: the owner's wildcard ACE and the seeded `owners-<id>` role group both cover it, so owners can export their own workspaces without any extra grant, and a **Deny** `export` ACE for **Everyone** on the workspace resource (positioned ahead of the wildcards) revokes it per workspace. Admins do **not** get export implicitly — they must be an owner or hold an explicit grant. See [Export & Import](../features/export-import.md#export).

### Collection- and admin-scoped permissions

Not every permission is checked on a workspace resource:

| Permission | Where it is checked | Controls                                        |
| ---------- | ------------------- | ----------------------------------------------- |
| `create`   | `/workspaces`       | Create/import workspaces                        |
| `admin`    | `/admin`            | Instance admin functions (`/admin/*` endpoints) |

## Checking Your Permissions

**Web UI**: the UI automatically shows/hides elements based on your permissions (admin button, workspace tabs, create button, etc.).

**API**: `GET /api/v1/my-permissions` returns your effective permissions on all static resources. Add `?resource=/workspaces/{id}` to check a specific resource.

**CLI**: `klangk ls --shared` shows workspaces shared with you.

## Troubleshooting: "Why can't I access this workspace?"

1. **Check your permissions**: `GET /api/v1/my-permissions?resource=/workspaces/{id}` — does it include the permission you need?
2. **Check the workspace ACL**: in the Sharing tab, expand "Advanced: Access Control" to see the ACE list.
3. **Check group membership**: are you in the right group? Admin > Groups tab shows group members.
4. **Check the ACL walk**: permissions are inherited from parent resources. An ACE on `/` applies to everything below it unless overridden. A `Deny` ACE at a higher level blocks access even if a lower-level `Allow` exists, if the `Deny` has a lower position number.
5. **Position matters**: ACEs are checked in position order (lowest first). If a `Deny` on position 0 matches before an `Allow` on position 1, access is denied. Use the ACL editor to reorder entries.
