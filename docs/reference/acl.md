# ACL System

Klangk uses an Access Control List (ACL) system to manage permissions. Instead of simple admin/non-admin roles, permissions are defined as ACL entries (ACEs) attached to resources in a tree hierarchy. This allows fine-grained control over who can do what, without code changes.

## Core Concepts

- **Resources**: paths in a tree that mirror the URL structure (`/`, `/workspaces`, `/workspaces/{id}`, `/admin`, `/admin/users`, etc.)
- **Principals**: who the ACE applies to — a specific user, a group, or a system principal (`Everyone` or `Authenticated`)
- **Permissions**: what action is allowed or denied (e.g., `view`, `create`, `edit`, `delete`, `terminal`, `files`, `chat`, `share`, `*`)
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
| `/workspaces` | Allow  | Authenticated | `create`   |
| `/admin`      | Allow  | group:admin   | `*`        |
| `/admin`      | Deny   | Everyone      | `*`        |

These defaults mean: any logged-in user can view pages and create workspaces; only members of the `admin` group can access admin functions; unauthenticated users are denied everything.

## Groups

Groups replace the old role system. A group is a named collection of users. The built-in `admin` group is created automatically on first startup and the default admin user is added to it.

**Admin UI**: Admin > Groups tab — create/delete groups, add/remove members.

**API endpoints**:

- `GET /api/v1/admin/groups` — list all groups
- `POST /api/v1/admin/groups` — create group `{"name": "...", "description": "..."}`
- `DELETE /api/v1/admin/groups/{id}` — delete group (cascades: removes all ACEs referencing it)
- `POST /api/v1/admin/groups/{id}/members` — add user `{"user_id": "..."}`
- `DELETE /api/v1/admin/groups/{id}/members/{user_id}` — remove user

## Workspace Permissions

When a workspace is created, the owner gets a `(Allow, user:{id}, *)` ACE on `/workspaces/{id}`. This grants full access: view, edit, delete, share, terminal, files, chat.

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) grants `view`, `terminal`, `files`, and `chat`. For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs.

**Tab visibility**: workspace tabs (Terminal, Files, Chat, Sharing, Settings) are gated by per-resource permissions. A shared user without `chat` permission won't see the Chat tab.

**Permissions checked on workspace resources**:

| Permission | Controls                                                          |
| ---------- | ----------------------------------------------------------------- |
| `view`     | Can see the workspace exists                                      |
| `terminal` | Can open a terminal / exec commands                               |
| `files`    | Can browse/upload/download files                                  |
| `chat`     | Can see the Chat tab                                              |
| `edit`     | Can change workspace settings (name, image, command, mounts, env) |
| `share`    | Can manage who has access (Sharing tab)                           |
| `delete`   | Can delete the workspace                                          |
| `*`        | All of the above                                                  |

## Checking Your Permissions

**Web UI**: the UI automatically shows/hides elements based on your permissions (admin button, workspace tabs, create button, etc.).

**API**: `GET /api/v1/my-permissions` returns your effective permissions on all static resources. Add `?resource=/workspaces/{id}` to check a specific resource.

**CLI**: `klangkc ls --shared` shows workspaces shared with you.

## Troubleshooting: "Why can't I access this workspace?"

1. **Check your permissions**: `GET /api/v1/my-permissions?resource=/workspaces/{id}` — does it include the permission you need?
2. **Check the workspace ACL**: in the Sharing tab, expand "Advanced: Access Control" to see the ACE list.
3. **Check group membership**: are you in the right group? Admin > Groups tab shows group members.
4. **Check the ACL walk**: permissions are inherited from parent resources. An ACE on `/` applies to everything below it unless overridden. A `Deny` ACE at a higher level blocks access even if a lower-level `Allow` exists, if the `Deny` has a lower position number.
5. **Position matters**: ACEs are checked in position order (lowest first). If a `Deny` on position 0 matches before an `Allow` on position 1, access is denied. Use the ACL editor to reorder entries.
