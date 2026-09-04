# ACL System

Klangk uses an Access Control List (ACL) system to manage permissions. Instead of simple admin/non-admin roles, permissions are defined as ACL entries (ACEs) attached to resources in a tree hierarchy. This allows fine-grained control over who can do what, without code changes.

## Core Concepts

- **Resources**: paths in a tree that mirror the URL structure (`/`, `/workspaces`, `/workspaces/{id}`, `/users`, `/groups`, etc.)
- **Principals**: who the ACE applies to — a specific user, a group, or a system principal (`Everyone` or `Authenticated`)
- **Permissions**: what action is allowed or denied — specific,
  self-describing names (`view`, `create-workspace`, `edit-workspace`,
  `terminal`, `files-view`, `share-workspace`, `*`)
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
├── /volumes                   (flat — the admin volume inventory)
└── /admin                     (marker only — nothing checks here)
```

## Default ACEs (seeded on first startup)

| Resource       | Action | Principal     | Permission               |
| -------------- | ------ | ------------- | ------------------------ |
| `/`            | Allow  | Authenticated | `view`                   |
| `/`            | Deny   | Everyone      | `*`                      |
| `/workspaces`  | Allow  | group:admins  | `create-workspace`       |
| `/workspaces`  | Allow  | group:members | `create-workspace`       |
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
| `/volumes`     | Allow  | group:admins  | `view-volumes`           |
| `/volumes`     | Allow  | group:admins  | `manage-volumes`         |
| `/images`      | Allow  | Authenticated | `view-images`            |
| `/images`      | Deny   | Everyone      | `*`                      |

These defaults mean: any logged-in user can view pages; every
member of the `members` group (i.e. every user — see below) can
create workspaces (#3137); only members of the `admins` group hold a
`manage-*` permission; unauthenticated users are denied everything.
There is no `/admin` resource — it is neither seeded nor checked by any
endpoint; instance-admin status derives from `admins`-group membership,
surfaced as the `is_admin` flag on `/api/v1/my-permissions`. The
`admins` group itself cannot be renamed or deleted.

The `/images` Allow Authenticated row is the deliberate exception to
the admin-default convention: the listing's consumers are the
workspace create/edit UIs, which non-admins reach with
`create-workspace`/`edit-workspace`. It is still an operator choice —
delete the row (default-deny then takes over) or scope it to a group in
the ACL editor. No trailing Deny Everyone row is seeded: no `/images`
route checks a permission other than `view-images`, and unauthenticated
requests are rejected by the JWT middleware before any ACL check.
(Side effect: without the row, authenticated users' effective
permissions on `/images` include the `view` inherited from `/` —
visible in `/my-permissions`; nothing checks it.)

`/volumes` is the one `manage-*` resource whose read side is split
out: the admin Volumes tab lists and deletes volumes, and a
read-only "volumes auditor" delegation makes sense, so
`GET /volumes` checks `view-volumes` while `POST`/`DELETE` check
`manage-volumes` — the action-specific naming pattern of
`/acl`+`manage-acls`, `/server`+`manage-server-schedule`, and
`/images`+`view-images`. The volume listing shows the deployment's
whole instance-managed inventory (the per-user creator label is
provenance, not an access filter). Deploys that want self-service
volumes back grant `manage-volumes` to `Authenticated` or a group via
the ACL editor. No trailing Deny Everyone row is seeded on `/volumes` either,
for the same reason.

### Workspace creation: default self-service, deny to restrict

By default every authenticated member can create workspaces (#3137):
the `members` group — which every new user joins automatically — holds
the seeded `create-workspace` Allow on `/workspaces`. Member footprint
is bounded by the admission and quota controls (admission memory
margin, `max_running_workspaces_per_user`, `volume_quota_per_user`),
and what a member-created workspace can run is constrained by
`allowed_images` / `allowed_mount_roots` / egress filtering.

A deployment that wants the pre-#3137 admin-only posture adds one
explicit Deny via the ACL editor (first-match-wins: position it ahead
of the members Allow):

1. Navigate to **Admin → ACL** (or the Advanced ACL editor).
2. Select the `/workspaces` resource.
3. Add a **Deny** entry for the `create-workspace` permission, targeting
   the **`members`** group (or the **Authenticated** system principal —
   this also covers users created outside the group), at a position
   **lower than** the members Allow row (lower position = checked
   first).

Alternatively grant creation to a narrower set: delete the members
Allow row, then add an **Allow** entry for `create-workspace` targeting
a specific **group** (e.g. a "developers" group you've created) or the
**Authenticated** system principal, positioned ahead of any Deny. The
create and import buttons in the web UI automatically appear/disappear
based on the user's effective `create-workspace` permission on
`/workspaces`.

## Groups

Groups replace the old role system. A group is a named collection of users. Two built-in groups are created automatically on first startup:

- **`admins`** — the default admin user is added to it; members can create workspaces and access admin functions.
- **`members`** — every new user (registration, invitation, OIDC first login, admin-created) is added automatically. Holds the seeded `create-workspace` Allow on `/workspaces` (#3137); a deploy that wants admin-only creation stages an explicit Deny ahead of it (see above).

**Admin UI**: Admin > Groups tab — create/delete groups, add/remove members.

**API endpoints**:

- `GET /api/v1/groups` — list all groups (authenticated)
- `POST /api/v1/groups` — create group `{"name": "...", "description": "..."}` (manage-groups)
- `DELETE /api/v1/groups/{id}` — delete group (cascades: removes all ACEs referencing it)
- `POST /api/v1/groups/{id}/members` — add user `{"user_id": "..."}`
- `DELETE /api/v1/groups/{id}/members/{user_id}` — remove user

### Per-workspace role groups

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

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) assigns the four role buckets — `owners`, `collaborators`, `coders`, `spectators` — which seed per-workspace role groups carrying: **coders** `monitor-workspace`, `join-workspace`, `terminal`, the lifecycle trio `start-workspace`/`stop-workspace`/`restart-workspace`, `egress-consent`, `code-in-isolation`, `exec-and-sync`, `spectate-on-shared-terminals`, `files-view`, `files-download`, `files-write`; **collaborators** the same plus `code-in-shared-terminals` and `share-terminals`; **spectators** `monitor-workspace`, `join-workspace`, `terminal` (the Terminal tab — it hosts the shared terminals they watch), and `spectate-on-shared-terminals`
(watch-only — no lifecycle control; the connect gate is split off `terminal` into `join-workspace`). For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs — gated on `share-advanced`, a separate power from `share-workspace`: rewriting the raw ACE list can grant `*` and add Deny entries, so a member who can invite collaborators does not thereby gain raw ACL editing or role assignment. Role-group writes (`POST`/`DELETE`/`PATCH /workspaces/{id}/roles*`) require `share-workspace` **and** `share-advanced` — the `owners-{id}` group holds the `*` wildcard, so assigning roles is an ACL change in effect. Owners hold `share-advanced` through their `*` wildcard ACE.

**Permissions checked on workspace resources**:

| Permission                     | Controls                                                                                                                                                                                                                                  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `view`                         | Can see the workspace exists                                                                                                                                                                                                              |
| `monitor-workspace`            | Can observe health/status: `GET /workspaces/{id}/status` and the `container_status` / `service_health` WebSocket frames                                                                                                                   |
| `join-workspace`               | Can open the workspace at all: the `workspace_connect` gate — without it the page never renders                                                                                                                                           |
| `start-workspace`              | Can start a stopped workspace container                                                                                                                                                                                                   |
| `stop-workspace`               | Can stop a running workspace container                                                                                                                                                                                                    |
| `restart-workspace`            | Can restart the container (HTTP route and the WS `restart_container` command)                                                                                                                                                             |
| `terminal`                     | Terminal tab visibility — without it the workspace renders with no Terminal tab (e.g. a files-only member); own terminals inside the tab still need `code-in-isolation`                                                                   |
| `code-in-isolation`            | Own terminal windows (the "+" tab in the web terminal and new `klangk shell` windows) — spectators get none                                                                                                                               |
| `code-in-shared-terminals`     | Can type into a shared terminal someone else started                                                                                                                                                                                      |
| `spectate-on-shared-terminals` | Can watch (read-only) shared terminals                                                                                                                                                                                                    |
| `share-terminals`              | Can share one of their terminals with workspace members (the `klangk terminal share` command / web UI toggle)                                                                                                                             |
| `egress-consent`               | Can decide held egress requests in interactive mode: register a decider (web Network tab, consent banner, `klangk consent-decide`), send verdicts, revoke, and pause prompting — owners/coders/collaborators by default, spectators never |
| `files-view`                   | Can browse file listings (metadata) and gets the Files tab; reading file bodies additionally needs `files-download`                                                                                                                       |
| `files-download`               | Can fetch file bytes: `/files/download` (raw stream/tar) and `/files/content` (text reader) — needs `files-view` too                                                                                                                      |
| `files-write`                  | Can mutate files: upload, rename, delete (needs `files-view` too)                                                                                                                                                                         |
| `exec-and-sync`                | Can run one-shot commands (`klangk exec`) and sync (`klangk sync`) against the workspace                                                                                                                                                  |
| `edit-workspace`               | Can change workspace settings (name, image, command, mounts, env)                                                                                                                                                                         |
| `share-workspace`              | Can manage who has access (Sharing tab: member and group shares)                                                                                                                                                                          |
| `share-advanced`               | Can rewrite the raw ACE list (`GET`/`PUT /workspaces/{id}/acl`, the Advanced ACL editor) and assign roles; owners hold it via `*`                                                                                                         |
| `delete-workspace`             | Can delete the workspace                                                                                                                                                                                                                  |
| `export-workspace`             | Can export the workspace as a `.tar.gz` archive                                                                                                                                                                                           |
| `duplicate-workspace`          | Can duplicate the workspace                                                                                                                                                                                                               |
| `transfer-workspace`           | Can transfer ownership to another user                                                                                                                                                                                                    |
| `*`                            | All of the above                                                                                                                                                                                                                          |

Withholding `files-download` hides every download affordance and returns
403 from both byte-moving endpoints: `/files/download` (raw stream or
tar.gz) and `/files/content` (the viewer's text reader). The file
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
(browsing itself is denied — the web UI's Files tab does not mount at all).

`export-workspace` is a workspace permission checked on `/workspaces/{id}`: the owner's wildcard ACE and the seeded `owners-<id>` role group both cover it, so owners can export their own workspaces without any extra grant, and a **Deny** `export-workspace` ACE for **Everyone** on the workspace resource (positioned ahead of the wildcards) revokes it per workspace. Admins do **not** get export implicitly — they must be an owner or hold an explicit grant. See [Export & Import](../features/export-import.md#export).

### First-class resource permissions

Every governed surface is a first-class top-level resource;
each carries a flat `manage-*` permission covering its actions. The
one split: `/volumes` also has a read-side `view-volumes` —
the listing gate of its admin tab, separate from `manage-volumes` so
a read-only volumes auditor can be delegated:

| Permission               | Where it is checked | Controls                                                                                                                                                              |
| ------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create-workspace`       | `/workspaces`       | Create/import workspaces                                                                                                                                              |
| `manage-users`           | `/users`            | The whole Users surface: list users and their workspaces, create, edit, unlock, delete, read active login sessions. `GET /users/search` stays authenticated (pickers) |
| `manage-groups`          | `/groups`           | The whole Groups surface: create, edit, delete, manage members. `GET /groups` stays authenticated (pickers)                                                           |
| `manage-invitations`     | `/invitations`      | List, send, resend, revoke invitations                                                                                                                                |
| `manage-server-schedule` | `/server`           | Server stop/recycle schedules: list, create, cancel                                                                                                                   |
| `manage-events`          | `/events`           | Read the container start/stop history (`GET /events`) — read-only audit                                                                                               |
| `manage-acls`            | `/acl`              | The Access Control browser: read and rewrite ACL entries on **any** resource via `GET/PUT /acl/*` — root-equivalent, see below                                        |
| `view-volumes`           | `/volumes`          | The volume inventory listing (`GET /volumes`) — the admin Volumes tab's gate; Allow group:admins by default                                                           |
| `manage-volumes`         | `/volumes`          | Create/delete instance-managed volumes (`POST /volumes`, `DELETE /volumes/{name}`) — Allow group:admins by default; the CLI's volume commands use it too              |
| `view-images`            | `/images`           | The image listing the create/edit UIs read (Allow Authenticated by default — the deliberate, ACL-editor-modifiable exception)                                         |
| `search-users`           | `/users`            | The member-picker type-ahead (`GET /users/search`) — Allow Authenticated by default                                                                                   |
| `admin`                  | `/admin`            | The instance-administrator **marker** only (`*` row); nothing checks it anywhere anymore (the transfer gate now checks `transfer-workspace`)                          |

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

Delegation is per-resource (the same recipe as the Events auditor): the admin group holds each `manage-*` via its seeded
Allow row.
To delegate a whole surface to a non-admin, add an `Allow` ACE for the
permission on that resource (Admin → Access Control) — it wins the ACL
walk ahead of the resource's Deny-everyone row. A delegated user then
gets the app-bar admin icon and the admin section showing exactly the
tabs their ACEs grant (e.g. `manage-users` on `/users`: the Users tab,
all of it). `GET /groups` and `GET /users/search` remain
authenticated-user reads (pickers, share dialogs).

Upgrading from an older deployment: migration 0021 inserts the six
Allow/Deny pairs for the admins group. If you had pre-staged rows on
one of those resources, the migration leaves it untouched. Migration
0029 appends the #3137 members `create-workspace` Allow after any
existing `/workspaces` rows — a **stock deployment (admins Allow
only) flips to self-service on upgrade**; a deployment with a staged
Deny keeps first-match-wins priority, so its admin-only posture
survives (the appended row is inert).

## WebSocket gates

The WebSocket layer enforces its own gates, separate from the REST
endpoints' `has_permission` dependencies. Full matrix — every WS
trigger, the permission it checks, the resource, who holds it by
default (the seeded role groups; owners hold `*`), and when it is
re-checked:

| WS trigger                                                              | Permission                                          | Resource           | Default holders (besides owner)   | Re-checked                                                                                          |
| ----------------------------------------------------------------------- | --------------------------------------------------- | ------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------- |
| `workspace_connect` handshake (open workspace)                          | `join-workspace`                                    | `/workspaces/{id}` | coders, collaborators, spectators | once per connect; revocation answers the next connect with machine-readable `forbidden`             |
| `restart_container` message                                             | `restart-workspace`                                 | `/workspaces/{id}` | coders, collaborators             | live, per message                                                                                   |
| exec channel (`klangk exec` / sync)                                     | `exec-and-sync`                                     | `/workspaces/{id}` | coders, collaborators             | live per `exec_start`; input into an in-flight session is not re-checked (one-shot channel)         |
| own terminal creation                                                   | `code-in-isolation`                                 | `/workspaces/{id}` | coders, collaborators             | live, per message                                                                                   |
| own-window management (`terminal_new/select/close/rename/list_windows`) | `code-in-isolation`                                 | `/workspaces/{id}` | coders, collaborators             | live, per message; plain code-less refusal for join-only members and spectators                     |
| ssh-agent relay (`ssh_agent_start`)                                     | `code-in-isolation` **or** `exec-and-sync`          | `/workspaces/{id}` | coders, collaborators             | live, per message; both session kinds wire `SSH_AUTH_SOCK` to the relay socket                      |
| `share_window` (share an own terminal)                                  | `share-terminals`                                   | `/workspaces/{id}` | collaborators                     | live, per message; unsharing needs no permission                                                    |
| `join_shared_terminal` / `list_shared_terminals`                        | `spectate-on-shared-terminals`                      | `/workspaces/{id}` | coders, collaborators, spectators | live, per message                                                                                   |
| typing into a shared terminal                                           | `code-in-shared-terminals` **or** `share-terminals` | `/workspaces/{id}` | collaborators                     | once at join — frozen into the session's read-only flag, enforced per keystroke until detach/rejoin |
| creating/targeting/closing shared terminals (incl. one's own)           | `share-terminals`                                   | `/workspaces/{id}` | collaborators                     | live, per message                                                                                   |
| service-health fan-out (per transition)                                 | `monitor-workspace`                                 | `/workspaces/{id}` | coders, collaborators, spectators | per fan-out (revocation stops delivery on the next frame)                                           |
| service-health snapshot at registration                                 | `monitor-workspace`                                 | `/workspaces/{id}` | coders, collaborators, spectators | per snapshot                                                                                        |
| consent-decider WS, workspace-scoped                                    | `egress-consent`                                    | `/workspaces/{id}` | coders, collaborators             | once at registration (handshake)                                                                    |

There is no deploy-wide consent-decider handshake (decision A):
consent authority is strictly per-workspace (`egress-consent` on
`/workspaces/{id}`), and a handshake without a `workspace` param is
refused. The deploy-wide flavor was an instance-administrator override
standing in for absent workspace members — an operator escape hatch, not
a consent concept — and it was gated by `manage-server-schedule`, which
has nothing to do with consent. Removed rather than renamed: with no
admin backstop, a workspace is interactive exactly while one of its own
members (a client of the `klangk shell` popup decider or the web
workspace page, both of which register whenever the member holds
`egress-consent`) is connected; with none, it reverts to the static
allow-list (the documented fallback).

Audit conclusions: all 34 permission names in the vocabulary
are enforced somewhere (no dead names); every WS gate's permission
matches a seeded grant path. Revocation timing differs per row: the
"live, per message" rows re-resolve principals on every message, so
mid-session revocation bites at the next message (revoking
`exec-and-sync` mid-session is e2e-tested); the once-per-
handshake rows (connect, deciders) take effect on the next
connection; the shared-terminal write gate takes effect on the next
join. The UI-side counterpart: affordances for WS-gated actions
(share/spectate buttons, the restart button) follow the same
permissions via the workspace's `my-permissions` set.

## Checking Your Permissions

**Web UI**: the UI automatically shows/hides elements based on your permissions (admin button, workspace tabs, create button, etc.).

**API**: `GET /api/v1/my-permissions` returns your effective permissions on all static resources, plus an `is_admin` flag (true for members of the `admins` group). Add `?resource=/workspaces/{id}` to check a specific resource.

**CLI**: `klangk ls --shared` shows workspaces shared with you.

## Troubleshooting: "Why can't I access this workspace?"

1. **Check your permissions**: `GET /api/v1/my-permissions?resource=/workspaces/{id}` — does it include the permission you need?
2. **Check the workspace ACL**: in the Sharing tab, expand "Advanced: Access Control" to see the ACE list.
3. **Check group membership**: are you in the right group? Admin > Groups tab shows group members.
4. **Check the ACL walk**: permissions are inherited from parent resources. An ACE on `/` applies to everything below it unless overridden. A `Deny` ACE at a higher level blocks access even if a lower-level `Allow` exists, if the `Deny` has a lower position number.
5. **Position matters**: ACEs are checked in position order (lowest first). If a `Deny` on position 0 matches before an `Allow` on position 1, access is denied. Use the ACL editor to reorder entries.
