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
| `/workspaces` | Allow  | group:admins  | `create`   |
| `/groups`     | Allow  | group:admins  | `create`   |
| `/admin`      | Allow  | group:admins  | `*`        |
| `/admin`      | Deny   | Everyone      | `*`        |

These defaults mean: any logged-in user can view pages; only members of the `admins` group can create workspaces, create groups, or access admin functions; unauthenticated users are denied everything.

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

- **`admins`** — the default admin user is added to it; members can create workspaces and access admin functions.
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

When a workspace is created, the owner gets a `(Allow, user:{id}, *)` ACE on `/workspaces/{id}`. This grants full access: view, edit, delete, share, terminal, files, export — every permission, including `change-acls`.

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) assigns the four role buckets — `owners`, `collaborators`, `coders`, `spectators` (#2750) — which seed per-workspace role groups carrying: **coders** `monitor`, `terminal`, `egress-consent`, `code-in-isolation`, `exec-and-sync`, `spectate-on-shared-terminals`, `files`, `files-download`, `files-write`; **collaborators** the same plus `code-in-shared-terminals` and `share-terminals`; **spectators** `monitor`, `terminal`, `spectate-on-shared-terminals` (watch-only). For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs — gated on `change-acls`, a separate power from `share` (#2764): rewriting the raw ACE list can grant `*` and add Deny entries, so a member who can invite collaborators does not thereby gain raw ACL editing or role assignment. Role-group writes (`POST`/`DELETE`/`PATCH /workspaces/{id}/roles*`) require `share` **and** `change-acls` — the `owners-{id}` group holds the `*` wildcard, so assigning roles is an ACL change in effect. Owners hold `change-acls` through their `*` wildcard ACE.

**Permissions checked on workspace resources**:

| Permission                     | Controls                                                                                                                                                                                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `view`                         | Can see the workspace exists                                                                                                                                                                                                                      |
| `monitor`                      | Can observe health/status: `GET /workspaces/{id}/status` and the `container_status` / `service_health` WebSocket frames (#2783)                                                                                                                   |
| `terminal`                     | Can open a terminal / exec commands                                                                                                                                                                                                               |
| `code-in-isolation`            | Own terminal windows (the "+" tab in the web terminal and new `klangk shell` windows) — spectators get none                                                                                                                                       |
| `code-in-shared-terminals`     | Can type into a shared terminal someone else started                                                                                                                                                                                              |
| `spectate-on-shared-terminals` | Can watch (read-only) shared terminals                                                                                                                                                                                                            |
| `share-terminals`              | Can share one of their terminals with workspace members (the `klangk terminal share` command / web UI toggle)                                                                                                                                     |
| `egress-consent`               | Can decide held egress requests in interactive mode: register a decider (web Network tab, consent banner, `klangk consent-decide`), send verdicts, revoke, and pause prompting — owners/coders/collaborators by default, spectators never (#2883) |
| `files`                        | Can browse file listings (metadata) and gets the Files tab; reading file bodies additionally needs `files-download`                                                                                                                               |
| `files-download`               | Can fetch file bytes: `/files/download` (raw stream/tar) and `/files/content` (text reader) — needs `files` too                                                                                                                                   |
| `files-write`                  | Can mutate files: upload, rename, delete (needs `files` too)                                                                                                                                                                                      |
| `exec-and-sync`                | Can run one-shot commands (`klangk exec`) and sync (`klangk sync`) against the workspace                                                                                                                                                          |
| `edit`                         | Can change workspace settings (name, image, command, mounts, env)                                                                                                                                                                                 |
| `share`                        | Can manage who has access (Sharing tab: member and group shares)                                                                                                                                                                                  |
| `change-acls`                  | Can rewrite the raw ACE list (`GET`/`PUT /workspaces/{id}/acl`, the Advanced ACL editor) and assign roles (#2764); owners hold it via `*`                                                                                                         |
| `delete`                       | Can delete the workspace                                                                                                                                                                                                                          |
| `export`                       | Can export the workspace as a `.tar.gz` archive (#2707)                                                                                                                                                                                           |
| `*`                            | All of the above                                                                                                                                                                                                                                  |

Withholding `files-download` hides every download affordance and returns
403 from both byte-moving endpoints: `/files/download` (raw stream or
tar.gz) and `/files/content` (the viewer's text reader, #2713). The file
list itself keeps working — names, sizes, and mtimes stay visible — but
no file body can be read: text renderers show a permission-denied state
and binary renderers (image, PDF, video, spreadsheet), which fetch raw
bytes through the download endpoint, cannot render.

Withholding `files-write` disables every mutating route — upload
(`/files/upload`), rename (`/files/rename`), and delete (`DELETE
/files`) — so a `files`-only member can browse listings but not read
file bodies or modify the workspace. In the file viewer the matching affordances disappear: no
drag-and-drop zone or upload hints, no Rename/Delete in the context menu,
and text-editor renderers go read-only (their Save uploads through that
endpoint).

The download gate covers every files endpoint that moves file bytes
out of the workspace — byte-perfect, unbounded export (any file size,
whole directories as tar.gz) via `/files/download`, and lossy text
reads (up to 1 MB per file) via `/files/content`. A member with `files`
but not
`files-download` can browse listings and metadata only. To grant viewing
without bulk export there is no longer a middle setting: grant both
`files` + `files-download` (viewer fully works) or withhold `files`
(browsing itself is denied — the web UI's Files tab does not mount at
all, #2886).

`export` is a workspace permission checked on `/workspaces/{id}`: the owner's wildcard ACE and the seeded `owners-<id>` role group both cover it, so owners can export their own workspaces without any extra grant, and a **Deny** `export` ACE for **Everyone** on the workspace resource (positioned ahead of the wildcards) revokes it per workspace. Admins do **not** get export implicitly — they must be an owner or hold an explicit grant. See [Export & Import](../features/export-import.md#export).

### Collection- and admin-scoped permissions

Not every permission is checked on a workspace resource:

| Permission                                                   | Where it is checked                                  | Controls                                                                                                                                         |
| ------------------------------------------------------------ | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `create`                                                     | `/workspaces`                                        | Create/import workspaces                                                                                                                         |
| `admin`                                                      | `/admin`                                             | Legacy catch-all for instance admin functions — still gates the `/admin/groups*` endpoints (#2941 removes them) and the drain/consent decider WS |
| `container-events`                                           | `/admin/container-events`                            | Read the container start/stop history: `GET /admin/container-events` and the admin Events tab (#2923)                                            |
| `view-users`, `create-user`, `edit-user`, `delete-user`      | `/admin/users` (and `/admin/users/{id}` via walk-up) | Users tab: list users and their workspaces, create, edit (+ unlock), delete (#2940)                                                              |
| `view-sessions`                                              | `/admin/users`                                       | Read a user's active login sessions (source IPs, user agents) — deliberately separate from `view-users` (#2940)                                  |
| `view-invitations`, `create-invitation`, `delete-invitation` | `/admin/invitations`                                 | Invitations tab: list, send (+ resend), revoke (#2940)                                                                                           |
| `change-acls`                                                | `/admin/acl`                                         | Read and rewrite ACL entries via the Access Control browser (`GET/PUT /admin/acl/*`) (#2940)                                                     |
| `view-server-schedule`, `manage-server-schedule`             | `/admin/server`                                      | Server stop/recycle schedules: list vs create/cancel (#2940)                                                                                     |

`PUT /admin/acl/resource` additionally requires `change-acls` on the
target when that target is an individual workspace (`/workspaces/{id}`)
— the same resource-level gate as `PUT /workspaces/{id}/acl`, so a raw
ACE rewrite of a workspace always carries the workspace's own grant.
Collection and static resources (`/`, `/workspaces`, `/groups`,
`/admin/*`) are governed by the `change-acls` check on `/admin/acl`
(#2940).

Every per-tab admin permission follows the same delegation recipe as
`container-events` (#2923, generalized in #2940): the admin group holds
it via its `/admin` `*` wildcard, so admins see every tab out of the
box. To delegate a slice of the admin section to a non-admin, add an
`Allow` ACE for the permission on its sub-resource (Admin → Access
Control) — the more-specific resource wins the ACL walk ahead of the
`/admin` Deny-everyone entry. A delegated user then gets the app-bar
admin icon and the admin section showing exactly the tabs their ACEs
grant (e.g. `view-users` on `/admin/users` alone: the Users tab,
read-only). The Groups tab is the exception: it still rides the legacy
`/admin/groups` endpoints behind the full-admin gate until #2941
removes them in favor of `/groups`.

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
