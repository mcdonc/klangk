# ACL System

Klangk uses an Access Control List (ACL) system to manage permissions. Instead of simple admin/non-admin roles, permissions are defined as ACL entries (ACEs) attached to resources in a tree hierarchy. This allows fine-grained control over who can do what, without code changes.

## Core Concepts

- **Resources**: paths in a tree that mirror the URL structure (`/`, `/workspaces`, `/workspaces/{id}`, `/users`, `/groups`, etc.)
- **Principals**: who the ACE applies to — a specific user, a group, or a system principal (`Everyone` or `Authenticated`)
- **Permissions**: what action is allowed or denied — specific,
  self-describing names (`view`, `create-workspace`, `edit-workspace`,
  `terminal`, `files-view`, `share-workspace`, `*`; #2946)
- **ACEs**: `(Allow/Deny, principal, permission)` entries ordered by position on a resource
- **ACL walk**: when checking permission, the system walks from the target resource up to `/`, checking each node's ACEs in order. First match wins. If no match after reaching root, access is denied.

## Resource Tree

```text
/                              (root)
├── /workspaces                (workspace collection)
│   └── /workspaces/{id}       (specific workspace)
├── /users                     (flat — no per-id nodes)
├── /groups                    (flat)
├── /invitations               (flat)
├── /server                    (flat — lifecycle schedules)
├── /events                    (flat — read-only audit)
├── /acl                       (flat — the ACL editor itself)
└── /admin                     (marker only — nothing checks here, #2944)
```

## Default ACEs (seeded on first startup)

| Resource       | Action | Principal     | Permission               |
| -------------- | ------ | ------------- | ------------------------ |
| `/`            | Allow  | Authenticated | `view`                   |
| `/`            | Deny   | Everyone      | `*`                      |
| `/workspaces`  | Allow  | group:admins  | `create-workspace`       |
| `/users`       | Allow  | group:admins  | `manage-users`           |
| `/users`       | Allow  | Authenticated | `search-users`           |
| `/users`       | Deny   | Everyone      | `*`                      |
| `/groups`      | Allow  | group:admins  | `manage-groups`          |
| `/groups`      | Deny   | Everyone      | `*`                      |
| `/invitations` | Allow  | group:admins  | `manage-invitations`     |
| `/invitations` | Deny   | Everyone      | `*`                      |
| `/server`      | Allow  | group:admins  | `manage-server-schedule` |
| `/server`      | Deny   | Everyone      | `*`                      |
| `/events`      | Allow  | group:admins  | `manage-events`          |
| `/events`      | Deny   | Everyone      | `*`                      |
| `/acl`         | Allow  | group:admins  | `manage-acls`            |
| `/acl`         | Deny   | Everyone      | `*`                      |
| `/volumes`     | Allow  | Authenticated | `manage-volumes`         |
| `/volumes`     | Deny   | Everyone      | `*`                      |
| `/images`      | Allow  | Authenticated | `view-images`            |
| `/images`      | Deny   | Everyone      | `*`                      |
| `/admin`       | Allow  | group:admins  | `*` (admin marker only)  |
| `/admin`       | Deny   | Everyone      | `*`                      |

These defaults mean: any logged-in user can view pages; only members of
the `admins` group can create workspaces or hold a `manage-*`
permission; unauthenticated users are denied everything. `/admin`
checks nothing anymore (#2944) — its `*` row only marks "instance
administrator" for permission-map consumers.

### Granting workspace creation to non-admin users

By default only administrators can create workspaces (#2569). To allow
other users or groups to create workspaces, add an ACL entry on the
`/workspaces` collection resource via the web UI:

1. Navigate to **Admin → ACL** (or the Advanced ACL editor).
2. Select the `/workspaces` resource.
3. Add an **Allow** entry for the `create-workspace` permission, targeting either:
   - A specific **group** (e.g., a "developers" group you've created) — all
     members of that group can then create workspaces.
   - The **Authenticated** system principal — restores the pre-#2569
     behavior where any logged-in user can create workspaces.
4. Ensure the new entry's position is lower than any Deny entry on the
   same resource (lower position = checked first).

The create button in the web UI automatically appears/disappears based
on the user's effective `create-workspace` permission on `/workspaces`.

## Groups

Groups replace the old role system. A group is a named collection of users. Two built-in groups are created automatically on first startup:

- **`admins`** — the default admin user is added to it; members can create workspaces and access admin functions.
- **`members`** — every new user (registration, invitation, OIDC first login, admin-created) is added automatically. Has no permissions by default, but deployers can grant `create-workspace` on `/workspaces` to this group to let all members create workspaces.

**Admin UI**: Admin > Groups tab — create/delete groups, add/remove members.

**API endpoints**:

- `GET /api/v1/groups` — list all groups (authenticated)
- `POST /api/v1/groups` — create group `{"name": "...", "description": "..."}` (manage-groups)
- `DELETE /api/v1/groups/{id}` — delete group (cascades: removes all ACEs referencing it)
- `POST /api/v1/groups/{id}/members` — add user `{"user_id": "..."}`
- `DELETE /api/v1/groups/{id}/members/{user_id}` — remove user

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

When a workspace is created, the owner gets a `(Allow, user:{id}, *)` ACE on `/workspaces/{id}`. This grants full access — every permission, including `share-advanced` and `transfer-workspace`.

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) assigns the four role buckets — `owners`, `collaborators`, `coders`, `spectators` (#2750) — which seed per-workspace role groups carrying: **coders** `monitor-workspace`, `join-workspace`, `terminal`, the lifecycle trio `start-workspace`/`stop-workspace`/`restart-workspace`, `egress-consent`, `code-in-isolation`, `exec-and-sync`, `spectate-on-shared-terminals`, `files-view`, `files-download`, `files-write`; **collaborators** the same plus `code-in-shared-terminals` and `share-terminals`; **spectators** `monitor-workspace`, `join-workspace`, `terminal` (the Terminal tab — it hosts the shared terminals they watch), and `spectate-on-shared-terminals`
(watch-only — no lifecycle control, #2946; #2975 splits the connect gate off `terminal` into `join-workspace`). For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs — gated on `share-advanced`, a separate power from `share-workspace` (#2764): rewriting the raw ACE list can grant `*` and add Deny entries, so a member who can invite collaborators does not thereby gain raw ACL editing or role assignment. Role-group writes (`POST`/`DELETE`/`PATCH /workspaces/{id}/roles*`) require `share-workspace` **and** `share-advanced` — the `owners-{id}` group holds the `*` wildcard, so assigning roles is an ACL change in effect. Owners hold `share-advanced` through their `*` wildcard ACE.

**Permissions checked on workspace resources**:

| Permission                     | Controls                                                                                                                                                                                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `view`                         | Can see the workspace exists                                                                                                                                                                                                                      |
| `monitor-workspace`            | Can observe health/status: `GET /workspaces/{id}/status` and the `container_status` / `service_health` WebSocket frames (#2783)                                                                                                                   |
| `join-workspace`               | Can open the workspace at all: the `workspace_connect` gate (#2975) — without it the page never renders                                                                                                                                           |
| `start-workspace`              | Can start a stopped workspace container                                                                                                                                                                                                           |
| `stop-workspace`               | Can stop a running workspace container                                                                                                                                                                                                            |
| `restart-workspace`            | Can restart the container (HTTP route and the WS `restart_container` command)                                                                                                                                                                     |
| `terminal`                     | Terminal tab visibility (#2975) — without it the workspace renders with no Terminal tab (e.g. a files-only member); own terminals inside the tab still need `code-in-isolation`                                                                   |
| `code-in-isolation`            | Own terminal windows (the "+" tab in the web terminal and new `klangk shell` windows) — spectators get none                                                                                                                                       |
| `code-in-shared-terminals`     | Can type into a shared terminal someone else started                                                                                                                                                                                              |
| `spectate-on-shared-terminals` | Can watch (read-only) shared terminals                                                                                                                                                                                                            |
| `share-terminals`              | Can share one of their terminals with workspace members (the `klangk terminal share` command / web UI toggle)                                                                                                                                     |
| `egress-consent`               | Can decide held egress requests in interactive mode: register a decider (web Network tab, consent banner, `klangk consent-decide`), send verdicts, revoke, and pause prompting — owners/coders/collaborators by default, spectators never (#2883) |
| `files-view`                   | Can browse file listings (metadata) and gets the Files tab; reading file bodies additionally needs `files-download`                                                                                                                               |
| `files-download`               | Can fetch file bytes: `/files/download` (raw stream/tar) and `/files/content` (text reader) — needs `files-view` too                                                                                                                              |
| `files-write`                  | Can mutate files: upload, rename, delete (needs `files-view` too)                                                                                                                                                                                 |
| `exec-and-sync`                | Can run one-shot commands (`klangk exec`) and sync (`klangk sync`) against the workspace                                                                                                                                                          |
| `edit-workspace`               | Can change workspace settings (name, image, command, mounts, env)                                                                                                                                                                                 |
| `share-workspace`              | Can manage who has access (Sharing tab: member and group shares)                                                                                                                                                                                  |
| `share-advanced`               | Can rewrite the raw ACE list (`GET`/`PUT /workspaces/{id}/acl`, the Advanced ACL editor) and assign roles (#2764); owners hold it via `*`                                                                                                         |
| `delete-workspace`             | Can delete the workspace                                                                                                                                                                                                                          |
| `export-workspace`             | Can export the workspace as a `.tar.gz` archive (#2707)                                                                                                                                                                                           |
| `duplicate-workspace`          | Can duplicate the workspace                                                                                                                                                                                                                       |
| `transfer-workspace`           | Can transfer ownership to another user                                                                                                                                                                                                            |
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
/files`) — so a `files-view`-only member can browse listings but not read
file bodies or modify the workspace. In the file viewer the matching affordances disappear: no
drag-and-drop zone or upload hints, no Rename/Delete in the context menu,
and text-editor renderers go read-only (their Save uploads through that
endpoint).

The download gate covers every files endpoint that moves file bytes
out of the workspace — byte-perfect, unbounded export (any file size,
whole directories as tar.gz) via `/files/download`, and lossy text
reads (up to 1 MB per file) via `/files/content`. A member with `files-view`
but not
`files-download` can browse listings and metadata only. To grant viewing
without bulk export there is no longer a middle setting: grant both
`files` + `files-download` (viewer fully works) or withhold `files`
(browsing itself is denied — the web UI's Files tab does not mount at
all, #2886).

`export-workspace` is a workspace permission checked on `/workspaces/{id}`: the owner's wildcard ACE and the seeded `owners-<id>` role group both cover it, so owners can export their own workspaces without any extra grant, and a **Deny** `export-workspace` ACE for **Everyone** on the workspace resource (positioned ahead of the wildcards) revokes it per workspace. Admins do **not** get export implicitly — they must be an owner or hold an explicit grant. See [Export & Import](../features/export-import.md#export).

### First-class resource permissions

Every governed surface is a first-class top-level resource (#2944);
each carries **one** flat `manage-*` permission covering all of its
actions — no per-action splits:

| Permission               | Where it is checked | Controls                                                                                                                                                              |
| ------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create-workspace`       | `/workspaces`       | Create/import workspaces (#2946 rename)                                                                                                                               |
| `manage-users`           | `/users`            | The whole Users surface: list users and their workspaces, create, edit, unlock, delete, read active login sessions. `GET /users/search` stays authenticated (pickers) |
| `manage-groups`          | `/groups`           | The whole Groups surface: create, edit, delete, manage members. `GET /groups` stays authenticated (pickers)                                                           |
| `manage-invitations`     | `/invitations`      | List, send, resend, revoke invitations                                                                                                                                |
| `manage-server-schedule` | `/server`           | Server stop/recycle schedules: list, create, cancel — plus the drain/consent decider WS handshake                                                                     |
| `manage-events`          | `/events`           | Read the container start/stop history (`GET /events`) — read-only audit                                                                                               |
| `manage-acls`            | `/acl`              | The Access Control browser: read and rewrite ACL entries on **any** resource via `GET/PUT /acl/*` — root-equivalent, see below                                        |
| `manage-volumes`         | `/volumes`          | Self-service volumes (still label-scoped to the caller at runtime) — Allow Authenticated by default (#2946)                                                           |
| `view-images`            | `/images`           | The image/nix/sudo capability listing the create/edit UIs read (#2946)                                                                                                |
| `search-users`           | `/users`            | The member-picker type-ahead (`GET /users/search`) — Allow Authenticated by default (#2946)                                                                           |
| `admin`                  | `/admin`            | The instance-administrator **marker** only (`*` row); nothing checks it anywhere anymore (#2944, #2946 — the transfer gate now checks `transfer-workspace`)           |

`PUT /acl/resource` additionally requires `share-advanced` on the target
when that target is an individual workspace (`/workspaces/{id}`) — the
same resource-level gate as `PUT /workspaces/{id}/acl`, so a raw ACE
rewrite of a workspace always carries the workspace's own grant.

**`manage-acls` is root-equivalent.** A holder can rewrite ACLs on any
resource — including `/` and the other first-class trees — so granting
it to a principal is granting instance-wide control, exactly like
adding them to the admins group. Grant it only to administrators.
(`share-advanced` on a workspace resource is the workspace-level gate;
`manage-acls` is the global editor — two different names, two
different resources.)

Delegation is per-resource (the same recipe as the Events auditor,
issue #2923): the admin group holds each `manage-*` via its seeded
Allow row.
To delegate a whole surface to a non-admin, add an `Allow` ACE for the
permission on that resource (Admin → Access Control) — it wins the ACL
walk ahead of the resource's Deny-everyone row. A delegated user then
gets the app-bar admin icon and the admin section showing exactly the
tabs their ACEs grant (e.g. `manage-users` on `/users`: the Users tab,
all of it). `GET /groups` and `GET /users/search` remain
authenticated-user reads (pickers, share dialogs).

Upgrading from a pre-#2944 deployment: migration 0021 inserts the six
Allow/Deny pairs for the admins group. If you had pre-staged rows on
one of those resources, the migration leaves it untouched.

## WebSocket gates (#2939)

The WebSocket layer enforces its own gates, separate from the REST
endpoints' `has_permission` dependencies. Full matrix — every WS
trigger, the permission it checks, the resource, who holds it by
default (the seeded role groups; owners hold `*`), and when it is
re-checked:

| WS trigger                                                    | Permission                                          | Resource           | Default holders (besides owner)   | Re-checked                                                                                          |
| ------------------------------------------------------------- | --------------------------------------------------- | ------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------- |
| `workspace_connect` handshake (open workspace)                | `join-workspace`                                    | `/workspaces/{id}` | coders, collaborators, spectators | once per connect; revocation answers the next connect with machine-readable `forbidden` (#2891)     |
| `restart_container` message                                   | `restart-workspace`                                 | `/workspaces/{id}` | coders, collaborators             | live, per message                                                                                   |
| exec channel (`klangk exec` / sync)                           | `exec-and-sync`                                     | `/workspaces/{id}` | coders, collaborators             | live per `exec_start`; input into an in-flight session is not re-checked (one-shot channel)         |
| own terminal creation                                         | `code-in-isolation`                                 | `/workspaces/{id}` | coders, collaborators             | live, per message                                                                                   |
| `share_window` (share an own terminal)                        | `share-terminals`                                   | `/workspaces/{id}` | collaborators                     | live, per message; unsharing needs no permission (#2875)                                            |
| `join_shared_terminal` / `list_shared_terminals`              | `spectate-on-shared-terminals`                      | `/workspaces/{id}` | coders, collaborators, spectators | live, per message                                                                                   |
| typing into a shared terminal                                 | `code-in-shared-terminals` **or** `share-terminals` | `/workspaces/{id}` | collaborators                     | once at join — frozen into the session's read-only flag, enforced per keystroke until detach/rejoin |
| creating/targeting/closing shared terminals (incl. one's own) | `share-terminals`                                   | `/workspaces/{id}` | collaborators                     | live, per message                                                                                   |
| service-health fan-out (per transition)                       | `monitor-workspace`                                 | `/workspaces/{id}` | coders, collaborators, spectators | per fan-out (revocation stops delivery on the next frame)                                           |
| service-health snapshot at registration                       | `monitor-workspace`                                 | `/workspaces/{id}` | coders, collaborators, spectators | per snapshot                                                                                        |
| consent-decider WS, workspace-scoped                          | `egress-consent`                                    | `/workspaces/{id}` | coders, collaborators             | once at registration (handshake)                                                                    |
| consent-decider WS, deploy-wide (drain)                       | `manage-server-schedule`                            | `/server`          | admins group                      | once at registration (handshake)                                                                    |

Audit conclusions (#2939): all 34 permission names in the vocabulary
are enforced somewhere (no dead names); every WS gate's permission
matches a seeded grant path. Revocation timing differs per row: the
"live, per message" rows re-resolve principals on every message, so
mid-session revocation bites at the next message (revoking
`exec-and-sync` mid-session is e2e-tested, #2706); the once-per-
handshake rows (connect, deciders) take effect on the next
connection; the shared-terminal write gate takes effect on the next
join. The UI-side counterpart: affordances for WS-gated actions
(share/spectate buttons, the restart button) follow the same
permissions via the workspace's `my-permissions` set.

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
