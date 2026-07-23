# API Endpoints

All HTTP and WebSocket endpoints, alphabetized by path. All REST paths
are under `/api/v1` except `/health`.

**Auth types**:

- **None** — public, no credentials required
- **JWT** — `Authorization: Bearer <access_token>` (user session)
- **ACL** — JWT + permission check on a resource (e.g. `view` on `/workspaces/{id}`)
- **Workspace JWT** — `Authorization: Bearer <workspace_token>` (container→host)

---

## Endpoints

### DELETE `/api/v1/admin/groups/{id}`

Delete a group (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/admin/groups/{id}/members/{user_id}`

Remove a user from a group (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/admin/invitations/{id}`

Revoke a pending invitation.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "revoked" }
```

---

### DELETE `/api/v1/admin/users/{id}`

Delete a user account. Cannot delete self or the system agent user.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "deleted" }
```

---

### POST `/api/v1/admin/users/{id}/unlockout`

Reset a user's login lockout, allowing them to log in immediately after
being locked out due to too many failed attempts.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "unlocked" }
```

---

### GET `/api/v1/admin/acl/by-principal/group/{id}`

List all ACL entries granted to a specific group across all resources.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 2,
    "permission": "view",
    "group_id": "uuid"
  }
]
```

---

### GET `/api/v1/admin/acl/by-principal/user/{id}`

List all ACL entries granted to a specific user across all resources.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 1,
    "permission": "terminal",
    "user_id": "uuid"
  }
]
```

---

### GET `/api/v1/admin/acl/resource`

Get the ACL entries for a specific resource. Query param: `resource`
(e.g. `/workspaces/uuid`).

**Auth:** JWT required. User must have `admin` permission on the
requested resource.

No request body.

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

---

### GET `/api/v1/admin/acl/tree`

Get a summary of the entire ACL tree across all resources.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  { "resource": "/workspaces/uuid", "ace_count": 3 },
  { "resource": "/groups/uuid", "ace_count": 1 }
]
```

---

### GET `/api/v1/admin/groups`

List all groups in the system (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  {
    "id": "uuid",
    "name": "my-group",
    "description": null,
    "created_at": "2025-01-01 12:00:00"
  }
]
```

---

### GET `/api/v1/admin/groups/{id}/members`

List members of a group (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/admin/invitations`

List all invitations (pending, accepted, and revoked).

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  {
    "id": "uuid",
    "email": "invited@example.com",
    "invited_by": "inviter-uuid",
    "invited_by_email": "admin@example.com",
    "status": "pending",
    "created_at": "2025-01-01 12:00:00",
    "accepted_at": null
  }
]
```

---

### GET `/api/v1/admin/users`

List all user accounts in the system.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
[
  {
    "id": "uuid",
    "email": "user@example.com",
    "verified": true,
    "provider": "local",
    "created_at": "2025-01-01 12:00:00",
    "groups": [{ "id": "uuid", "name": "admins" }]
  }
]
```

---

### GET `/api/v1/auth/me`

Get the current authenticated user's profile: their `id`, `email`, and
display `handle`. This resolves the bearer token's identity into a stable
user record; the frontend calls it to read the current user's identity
(e.g. the Settings page populates its handle field from the response).

**Auth:** JWT required.

No request body.

```json
{ "id": "uuid", "email": "user@example.com", "handle": "myhandle" }
```

The response carries identity fields only — it does **not** include
roles, groups, or per-resource permissions. For those, call
`GET /api/v1/my-permissions`, which returns the user's groups and their
effective permissions across resources.

---

### GET `/api/v1/auth/oidc/{provider_id}/callback`

OIDC callback endpoint. Called by the identity provider after
authentication. Validates the state cookie and exchanges the
authorization code for tokens.

**Auth:** None. Query params: `code`, `state`, optional `error`.

Returns HTTP 302 redirect to `/#/oidc-complete?token=...` or CLI localhost URL.

---

### GET `/api/v1/auth/oidc/{provider_id}/login`

Initiate OIDC login. Redirects the user to the identity provider's
authorization endpoint.

**Auth:** None. Optional query param: `cli_redirect` (must be localhost URL).

Returns HTTP 302 redirect to OIDC IdP.

---

### GET `/api/v1/auth/verify`

Verify a user's email address using the token from the verification
email. Returns a session token on success.

**Auth:** None. Query param: `token` (verification JWT from email link).

```json
{ "status": "verified", "access_token": "jwt-string" }
```

---

### GET `/api/v1/auth/verify-workspace-token`

Validate a workspace JWT. Used internally by the proxy's `auth_request` to
gate container-to-host traffic.

**Auth:** Workspace JWT required (`Authorization: Bearer <workspace_token>`).

```json
{ "status": "ok", "workspace_id": "uuid" }
```

On failure: 401 with `X-Token-Error` header (`missing`, `expired`, or `invalid`).

---

### GET `/api/v1/config`

Get public instance configuration: whether registration and invitations
are enabled, available OIDC providers, login banner text, and feature
frontend config.

**Auth:** None.

No request body.

```json
{
  "registration_enabled": true,
  "invitations_enabled": true,
  "login_banner_title": "",
  "login_banner": "",
  "oidc_providers": [],
  "auth_modes": "both",
  "instance_id": "string"
}
```

`auth_modes` is a string — one of `password`, `oidc`, `both`, or `none`
(no-login single-user mode). The frontend and CLI branch on this value;
see [Auth Modes](../features/auth-modes.md).

---

### GET `/api/v1/groups`

List all groups visible to the current user.

**Auth:** JWT required.

No request body.

```json
[
  {
    "id": "uuid",
    "name": "my-group",
    "description": null,
    "created_at": "2025-01-01 12:00:00"
  }
]
```

---

### GET `/api/v1/groups/{id}/members`

List the members of a group.

**Auth:** JWT required. User must have `view` permission on `/groups/{id}`.

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/images`

List available container images that can be used when creating or
editing workspaces.

**Auth:** JWT required.

No request body.

```json
{ "default": "image-name:tag", "allowed": ["image1:tag", "image2:tag"] }
```

---

### GET `/api/v1/my-permissions`

Get the current user's effective permissions. If a `resource` query param
is provided, returns permissions for that specific resource; otherwise
returns permissions across all static resources.

**Auth:** JWT required. Optional query param: `resource`.

No request body.

```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "groups": [],
  "permissions": {
    "/workspaces/uuid": ["view", "terminal", "files", "chat"]
  }
}
```

---

### GET `/api/v1/users/search`

Search for users by email or handle. Used for autocomplete when sharing
workspaces or adding group members.

**Auth:** JWT required. Query param: `q` (search string, min length 1).

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/version`

Get the build version, git commit, build timestamp, and list of
installed features.

**Auth:** None.

No request body.

```json
{
  "version": "1.2.3",
  "commit": "abc1234",
  "built_at": "2026-06-21T00:00:00Z",
  "features": []
}
```

---

### GET `/api/v1/volumes`

List podman volumes owned by the current user.

**Auth:** JWT required.

No request body.

```json
[{ "name": "my-volume", "created": "2025-01-01T12:00:00Z" }]
```

---

### GET `/api/v1/workspaces`

List workspaces owned by the current user.

**Auth:** JWT required.

No request body.

**Query params (optional, pagination):**

| Param    | Type   | Default   | Constraints           |
| -------- | ------ | --------- | --------------------- |
| `limit`  | int    | (none)    | `1`–`100`             |
| `offset` | int    | (none)    | `>= 0`                |
| `sort`   | string | `created` | `name` \| `created`   |
| `order`  | string | `desc`    | `asc` \| `desc`       |
| `q`      | string | (none)    | name substring filter |

Without pagination params the endpoint returns a **bare list** (backward
compatible). With `?limit=` and/or `?offset=` it returns a **pagination
envelope** `{ items, has_more, next_offset }`. `sort`/`order`/`q` apply in
both shapes.

Sorting is whitelisted (`created`→`created_at`, `name`→`name`) with an `id`
tiebreaker so offset pagination is deterministic. `q` matches anywhere in
the name (`LIKE '%q%'`), not just a prefix, and is applied before pagination
so `has_more`/`next_offset` reflect the filtered set.

```json
[
  {
    "id": "uuid",
    "name": "my-workspace",
    "container_id": null,
    "image": null,
    "service_command": null,
    "mounts": null,
    "env": null,
    "created_at": "2025-01-01 12:00:00"
  }
]
```

Paginated response (`?limit=10&offset=0`):

```json
{
  "items": [
    /* workspace objects as above */
  ],
  "has_more": true,
  "next_offset": 10
}
```

`has_more` is `true` when the returned page is full (`len(items) == limit`);
`next_offset` is `offset + limit` when more rows remain, otherwise `null`.

---

### GET `/api/v1/workspaces/shared`

List workspaces that other users have shared with the current user.

**Auth:** JWT required.

No request body.

**Query params (optional, pagination):** same `?limit=` / `?offset=` as
`GET /api/v1/workspaces`, plus `?sort=name|created`, `?order=asc|desc`, and
`?q=<substring>` (name substring). Without params returns a bare list, with
params returns the `{ items, has_more, next_offset }` envelope.

```json
[
  {
    "id": "uuid",
    "name": "shared-workspace",
    "container_id": null,
    "image": null,
    "service_command": null,
    "mounts": null,
    "env": null,
    "created_at": "2025-01-01 12:00:00",
    "owner_email": "owner@example.com"
  }
]
```

---

### GET `/api/v1/workspaces/{id}/acl`

Get the resolved ACL entries for a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

---

### GET `/api/v1/workspaces/{id}/export`

Export a workspace as a `.tar.gz` archive. The archive contains the
workspace configuration and container filesystem.

**Auth:** JWT required. User must have `admin` permission on the
requested resource.

No request body. Returns `StreamingResponse` (`.tar.gz` binary stream).
Headers: `Content-Disposition: attachment; filename="<name>.tar.gz"`,
`X-Estimated-Size: <bytes>`.

---

### GET `/api/v1/workspaces/{id}/files`

List files and directories inside the workspace container. Requires a
running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`. Query param: `path` (absolute container path,
default `/`).

No request body.

```json
[
  {
    "name": "README.md",
    "path": "/home/work/README.md",
    "is_dir": false,
    "size": 1024,
    "mtime": 1704067200.0,
    "ctime": 1704067200.0
  }
]
```

---

### GET `/api/v1/workspaces/{id}/files/content`

Read the contents of a file inside the workspace container. Requires a
running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`. Query param: `path` (absolute container path).

No request body.

```json
{ "path": "src/main.py", "content": "file contents as string" }
```

---

### GET `/api/v1/workspaces/{id}/files/download`

Download a file or directory from the workspace container. Single files
are streamed directly; directories are streamed as `.tar.gz` archives.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`. Query param: `path` (absolute container path).

No request body. Returns a streamed `application/octet-stream` (single
file) or `application/gzip` (directory archive).

---

### GET `/api/v1/workspaces/{id}/groups`

List groups that have been granted access to a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
[{ "id": "uuid", "name": "group-name" }]
```

---

### GET `/api/v1/workspaces/{id}/members`

List individual users who have been granted access to a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/workspaces/{id}/roles`

List role groups for a workspace and their members. Each workspace has
four roles: `owners`, `coders`, `collaborators`, `spectators`.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
[
  {
    "role": "owners",
    "group_id": "uuid",
    "group_name": "owners-uuid",
    "members": [{ "id": "uuid", "email": "user@example.com" }]
  }
]
```

---

### PATCH `/api/v1/admin/groups/{id}`

Update a group's name or description (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

```json
{ "name": "new-name", "description": "updated description" }
```

```json
{ "status": "updated" }
```

---

### PATCH `/api/v1/admin/users/{id}`

Update a user's email, password, or handle (admin). All fields optional.

**Auth:** JWT required. User must have `admin` permission on `/`.

```json
{ "email": "new@example.com", "password": "newpass", "handle": "newhandle" }
```

```json
{ "status": "updated" }
```

---

### PATCH `/api/v1/groups/{id}`

Update a group's name or description.

**Auth:** JWT required. User must have `edit` permission on `/groups/{id}`.

```json
{ "name": "new-name", "description": "updated description" }
```

```json
{ "status": "updated" }
```

---

### PATCH `/api/v1/workspaces/{id}/roles`

Change a user's role in a workspace. Set `role` to `null` to remove the
user from all roles.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

```json
{ "email": "user@example.com", "role": "coders" }
```

```json
{ "ok": true, "email": "user@example.com", "role": "coders" }
```

---

### POST `/api/v1/admin/groups`

Create a new group (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

```json
{ "name": "my-group", "description": "optional description" }
```

```json
{ "id": "uuid", "name": "my-group", "description": "optional description" }
```

---

### POST `/api/v1/admin/groups/{id}/members`

Add a user to a group (admin).

**Auth:** JWT required. User must have `admin` permission on `/`.

```json
{ "user_id": "uuid" }
```

```json
{ "status": "added" }
```

---

### POST `/api/v1/admin/invitations`

Send an invitation email to a new user.

**Auth:** JWT required. User must have `admin` permission on `/`.

```json
{ "email": "user@example.com" }
```

```json
{ "id": "uuid", "email": "user@example.com", "status": "pending" }
```

---

### POST `/api/v1/admin/invitations/{id}/resend`

Resend an invitation email.

**Auth:** JWT required. User must have `admin` permission on `/`.

No request body.

```json
{ "status": "resent" }
```

---

### POST `/api/v1/admin/users`

Create a new user account. By default the user is created verified with
the given password. Set `send_verification_email` to `true` to create
the user unverified and send a verification email so they can set their
own password (the `password` field is ignored in this case).

**Auth:** JWT required. User must have `admin` permission on `/`.

With password (default):

```json
{ "email": "user@example.com", "password": "secretpass" }
```

```json
{ "id": "uuid", "email": "user@example.com", "status": "created" }
```

With verification email:

```json
{ "email": "user@example.com", "send_verification_email": true }
```

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "status": "pending_verification"
}
```

---

### POST `/api/v1/auth/accept-invite`

Accept an invitation and create a new account. The token is from the
invitation email.

**Auth:** None.

```json
{ "token": "invitation-jwt", "password": "newpassword" }
```

```json
{ "status": "accepted", "access_token": "jwt-string" }
```

---

### POST `/api/v1/auth/change-email`

Change the current user's email address. Requires the current password
for verification. The account is marked unverified until the new email
is confirmed.

**Auth:** JWT required.

```json
{ "email": "new@example.com", "password": "currentpassword" }
```

```json
{ "status": "updated", "needs_verification": true }
```

---

### POST `/api/v1/auth/change-handle`

Change the current user's display handle. Requires the current password
for verification.

**Auth:** JWT required.

```json
{ "handle": "newhandle", "password": "currentpassword" }
```

```json
{ "status": "updated", "handle": "newhandle" }
```

---

### POST `/api/v1/auth/change-password`

Change the current user's password. Requires the current password.

**Auth:** JWT required.

```json
{ "current_password": "oldpass", "new_password": "newpass" }
```

```json
{ "status": "updated" }
```

---

### POST `/api/v1/auth/forgot-password`

Request a password reset email. Always returns success even if the email
doesn't exist (prevents user enumeration). 60s rate limit per email.

**Auth:** None.

```json
{ "email": "user@example.com" }
```

```json
{ "status": "sent" }
```

---

### POST `/api/v1/auth/login`

Authenticate with an email **or** handle plus a password. Returns a JWT
access token.

**Auth:** None. Rate-limited (see Rate Limiting below).

```json
{ "identifier": "user@example.com", "password": "secretpass" }
```

The `identifier` may be a user's email address (e.g.
`user@example.com`) or their handle (e.g. `alice`); the two are
disambiguated by the presence of `@`. Login brute-force lockout is keyed
on the resolved user's canonical email, so attempts under either form
share one counter.

```json
{ "access_token": "jwt-string", "token_type": "bearer" }
```

---

### POST `/api/v1/auth/local`

No-login single-user mode: mint a JWT for the seeded default user with no
credentials. Only available when `KLANGK_AUTH_MODES=none`; returns **403**
otherwise. See [Auth Modes](../features/auth-modes.md).

**Auth:** None. Reachable only from loopback (the `KLANGK_LISTEN` bind plus an
the proxy's `allow 127.0.0.1/::1; deny all` per-location ACL keep it unreachable from
workspace containers).

No request body.

```json
{
  "access_token": "jwt-string",
  "token_type": "bearer",
  "email": "admin@example.com"
}
```

The `email` field lets the CLI key its cached credentials (it stores tokens
per user). The token is indistinguishable from a password-login token to the
refresh and blocklist machinery.

---

### POST `/api/v1/auth/logout`

Log out the current session. Blocklists the token's JTI so it cannot be
reused.

**Auth:** JWT required.

No request body.

```json
{ "status": "ok" }
```

For OIDC users with logout redirect configured:

```json
{ "status": "ok", "oidc_logout_url": "https://idp.example.com/logout?..." }
```

---

### POST `/api/v1/auth/refresh`

Exchange the current JWT for a new one. The old token's JTI is
blocklisted.

**Auth:** JWT required.

No request body.

```json
{ "access_token": "new-jwt-string", "token_type": "bearer" }
```

---

### POST `/api/v1/auth/register`

Create a new user account. A verification email is sent unless running
in test mode. Can be disabled via `KLANGK_DISABLE_REGISTRATION`.

**Auth:** None.

```json
{ "email": "user@example.com", "password": "secretpass" }
```

With email verification:

```json
{ "status": "pending_verification", "email": "user@example.com" }
```

In test mode (auto-verified):

```json
{ "user_id": "uuid", "email": "user@example.com", "access_token": "jwt-string" }
```

---

### POST `/api/v1/auth/resend-verification`

Resend the email verification link. Requires the account password. 60s
rate limit per email.

**Auth:** None.

```json
{ "email": "user@example.com", "password": "secretpass" }
```

```json
{ "status": "sent" }
```

---

### POST `/api/v1/auth/reset-password`

Set a new password using the token from a password reset email. Returns
a session token (auto-login after reset).

**Auth:** None.

```json
{ "token": "reset-jwt-from-email", "password": "newpassword" }
```

```json
{ "status": "reset", "access_token": "jwt-string" }
```

---

### POST `/api/v1/browser-delegate`

Relay a request from a workspace container to a connected browser tab.
Used by Pi extensions that need to interact with the user's browser
(e.g. navigating, reading page content).

**Auth:** Workspace JWT required + proxy IP ACL (container traffic only).

```json
{ "action": "navigate", "browser_id": "string" }
```

Returns forwarded result from the target browser tab (arbitrary JSON).

---

### POST `/api/v1/browser-delegate/stream`

Streaming variant of browser-delegate. Returns results as newline-
delimited JSON chunks.

**Auth:** Workspace JWT required + proxy IP ACL (container traffic only).

```json
{ "action": "string", "browser_id": "string" }
```

Returns `StreamingResponse` (`application/x-ndjson`).

---

### POST `/api/v1/groups`

Create a new group.

**Auth:** JWT required. User must have `create` permission on `/groups`.

```json
{ "name": "my-group", "description": "optional description" }
```

```json
{ "id": "uuid", "name": "my-group", "description": "optional description" }
```

---

### POST `/api/v1/groups/{id}/members`

Add a user to a group.

**Auth:** JWT required. User must have `manage_members` permission on
`/groups/{id}`.

```json
{ "user_id": "uuid" }
```

```json
{ "status": "added" }
```

---

### POST `/api/v1/volumes`

Create a new podman volume labeled with the current user's ID.

**Auth:** JWT required.

```json
{ "name": "my-volume" }
```

```json
{ "name": "my-volume", "created": "2026-06-21T00:00:00Z" }
```

---

### POST `/api/v1/workspaces`

Create a new workspace. The creating user becomes the owner with full
ACL permissions. Role groups (owners, coders, collaborators, spectators)
are created automatically.

**Auth:** JWT required.

```json
{
  "name": "my-workspace",
  "image": "klangk-workspace:latest",
  "service_command": "/bin/bash",
  "mounts": ["my-volume:/home/user/data"],
  "env": { "MY_VAR": "value" }
}
```

All fields except `name` are optional.

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "name": "my-workspace",
  "image": null,
  "service_command": null,
  "mounts": null,
  "env": null,
  "num_ports": 5,
  "created_at": "2025-01-01 12:00:00"
}
```

---

### POST `/api/v1/workspaces/import`

Create a new workspace from a previously exported `.tar.gz` archive.
Environment variables are sanitized during import.

**Auth:** JWT required. Multipart form upload: `file` (`.tar.gz` archive),
optional `name` form field.

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "name": "my-workspace",
  "image": null,
  "service_command": null,
  "mounts": null,
  "env": null,
  "num_ports": 5,
  "created_at": "2025-01-01 12:00:00"
}
```

---

### POST `/api/v1/workspaces/post-chat-message`

Post a chat message from a workspace container to the workspace's chat
channel. Used by Pi extensions and tools running inside the container.

**Auth:** Workspace JWT required + proxy IP ACL (container traffic only).

```json
{ "message": "text of message" }
```

```json
{
  "id": "uuid",
  "workspace_id": "uuid",
  "sender": "agent",
  "sender_id": "agent",
  "text": "text of message",
  "message_type": 2
}
```

---

### POST `/api/v1/workspaces/{id}/duplicate`

Clone an existing workspace's configuration into a new workspace.

**Auth:** JWT required. User must have `create` permission on
`/workspaces/{id}`.

```json
{ "name": "cloned-workspace" }
```

```json
{
  "id": "uuid",
  "user_id": "uuid",
  "name": "my-workspace",
  "image": null,
  "service_command": null,
  "mounts": null,
  "env": null,
  "num_ports": 5,
  "created_at": "2025-01-01 12:00:00"
}
```

---

### POST `/api/v1/workspaces/{id}/files/rename`

Rename or move a file or directory inside the workspace container.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`.

```json
{ "old_path": "/home/work/old.py", "new_path": "/home/work/new.py" }
```

```json
{ "path": "/home/work/new.py", "status": "renamed" }
```

---

### POST `/api/v1/workspaces/{id}/files/upload`

Upload a file into the workspace container. Default 500 MB limit.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`. Multipart form: `file` (upload), optional `path`
query param (absolute container path).

```json
{ "path": "/home/work/uploads/file.txt", "status": "uploaded" }
```

---

### POST `/api/v1/workspaces/{id}/groups`

Grant a group access to a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

```json
{ "group_id": "uuid" }
```

```json
{ "status": "shared", "group_id": "uuid", "name": "group-name" }
```

---

### POST `/api/v1/workspaces/{id}/members`

Grant a user access to a workspace. The user receives view, terminal,
files, and chat permissions.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

```json
{ "email": "user@example.com" }
```

```json
{ "status": "shared", "user_id": "uuid", "email": "user@example.com" }
```

---

### POST `/api/v1/workspaces/{id}/restart`

Restart a workspace by stopping and removing its container. The
container is recreated on the next connection.

**Auth:** JWT required. User must have `terminal` permission on
`/workspaces/{id}`.

No request body.

```json
{ "status": "restarted" }
```

---

### GET `/api/v1/workspaces/{id}/status`

Return the container status for a workspace.

**Auth:** JWT required. User must have `terminal` permission on
`/workspaces/{id}`.

No request body.

**Response when running:**

```json
{
  "running": true,
  "container_id": "abc123...",
  "health": null,
  "health_message": null,
  "idle_seconds": 42.5,
  "idle_timeout": 1800,
  "ports": [9000, 9001]
}
```

**Response when stopped:**

```json
{
  "running": false,
  "container_id": null,
  "health": null,
  "health_message": null,
  "idle_seconds": null,
  "idle_timeout": null,
  "ports": []
}
```

The `health` field is the check status (`"healthy"`, `"unhealthy"`, or
`null` when no check is configured or no container is running). When
unhealthy, `health_message` carries a bounded tail of the check's
stderr/stdout explaining _why_ it failed (`null` otherwise) — so a
failing check isn't a black box.

---

### POST `/api/v1/workspaces/{id}/roles/{role}`

Add a user to a workspace role. Valid roles: `owners`, `coders`,
`collaborators`, `spectators`.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

```json
{ "email": "user@example.com" }
```

```json
{ "ok": true }
```

---

### PUT `/api/v1/admin/acl/resource`

Replace all ACL entries for a specific resource. Query param: `resource`.

**Auth:** JWT required. User must have `admin` permission on the
requested resource.

```json
[
  {
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

`action`: 0=deny, 1=allow. `principal_type`: 0=system, 1=user, 2=group.

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

---

### PUT `/api/v1/workspaces/{id}`

Update a workspace's configuration (name, container image, default
command, volume mounts, environment variables). All fields optional.

**Auth:** JWT required. User must have `edit` permission on
`/workspaces/{id}`.

```json
{
  "name": "renamed",
  "image": "klangk-workspace:latest",
  "service_command": "/bin/bash",
  "mounts": ["vol:/data"],
  "env": { "KEY": "VALUE" }
}
```

```json
{ "status": "updated" }
```

---

### PUT `/api/v1/workspaces/{id}/acl`

Replace all ACL entries for a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

```json
[
  {
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

```json
[
  {
    "resource": "/workspaces/uuid",
    "action": 1,
    "principal_type": 1,
    "permission": "view",
    "user_id": "uuid",
    "group_id": null,
    "system_principal": null
  }
]
```

---

### DELETE `/api/v1/groups/{id}`

Delete a group.

**Auth:** JWT required. User must have `delete` permission on `/groups/{id}`.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/groups/{id}/members/{user_id}`

Remove a user from a group.

**Auth:** JWT required. User must have `manage_members` permission on
`/groups/{id}`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/volumes/{name}`

Delete a podman volume. Only the owning user can delete their volumes.

**Auth:** JWT required. Checks user ownership.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/workspaces/{id}`

Delete a workspace and stop its container.

**Auth:** JWT required. User must have `delete` permission on
`/workspaces/{id}`.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/workspaces/{id}/files`

Delete a file or directory inside the workspace container. Requires a
running container (returns 409 if stopped). Query param: `path`
(absolute container path).

**Auth:** JWT required. User must have `files` permission on
`/workspaces/{id}`.

No request body.

```json
{ "path": "src/old.py", "status": "deleted" }
```

---

### DELETE `/api/v1/workspaces/{id}/groups/{group_id}`

Revoke a group's access to a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/workspaces/{id}/members/{member_id}`

Revoke a user's access to a workspace.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/workspaces/{id}/roles/{role}/{member_id}`

Remove a user from a workspace role.

**Auth:** JWT required. User must have `share` permission on `/workspaces/{id}`.

No request body.

```json
{ "ok": true }
```

---

### GET `/health`

Readiness check. Returns OK if the server is running.

**Auth:** None.

No request body.

```json
{ "status": "ok" }
```

---

### WebSocket `/ws`

Primary WebSocket connection for real-time communication. Handles
terminal I/O, chat messages, workspace status updates, and browser
delegate events.

**Auth:** JWT required via `?token=` query param.

Close codes: 4001 (missing/invalid token), 4002 (expired token).

---

## Test-Only Endpoints

Available only when `KLANGK_TEST_MODE` is set. No auth required.

### GET `/api/v1/test/browsers/{id}`

List browser registrations for a workspace.

```json
[{ "browser_id": "string", "email": "user@example.com" }]
```

### GET `/api/v1/test/idle-timeout`

Get the idle timeout for a workspace. Query param: `workspace_id`.

```json
{ "idle_timeout_seconds": 300 }
```

### POST `/api/v1/test/set-idle-timeout`

Override the idle timeout for a workspace (or globally).

```json
{ "seconds": 60, "workspace_id": "uuid" }
```

```json
{ "idle_timeout_seconds": 60 }
```

### GET `/api/v1/test/workspace-token/{id}`

Generate a workspace JWT for testing container-to-host endpoints.

```json
{ "token": "workspace-jwt-string" }
```

---

## Rate Limiting

### Login Brute-Force Protection

Enabled by default (5 failed attempts → lockout). Configure via environment variables:

- `KLANGK_LOGIN_LOCKOUT_FAILURES` (default `5`) —
  attempts before lockout (0 = disabled)
- `KLANGK_LOGIN_LOCKOUT_DURATION` (default `900`) —
  lockout period in seconds
- `KLANGK_LOGIN_LOCKOUT_WINDOW` (default `300`) —
  attempt counting window in seconds

### Email Rate Limiting

- Verification resend: 60s per email (in-memory)
- Password reset: 60s per email (in-memory)
