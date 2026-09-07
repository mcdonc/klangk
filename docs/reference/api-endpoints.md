# API Endpoints

All HTTP and WebSocket endpoints, alphabetized by path. All REST paths
are under `/api/v1` except `/audit`, `/health`, `/empty`, and the
[`/llm-proxy/*`](#llm-proxy-endpoints) routes.

**Auth types**:

- **None** — public, no credentials required
- **JWT** — `Authorization: Bearer <access_token>` (user session)
- **ACL** — JWT + permission check on a resource (e.g. `view` on `/workspaces/{id}`)
- **Workspace JWT** — `Authorization: Bearer <workspace_token>` (container→host)

---

## Endpoints

### DELETE `/api/v1/groups/{id}`

Delete a group (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/groups/{id}/members/{user_id}`

Remove a user from a group (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/server/schedule/{schedule_id}`

Cancel a pending server stop/recycle schedule. All connected clients' countdowns update immediately.

**Auth:** JWT required. User must have the `manage-server-schedule` permission on `/server`.

No request body.

```json
{ "cancelled": "uuid" }
```

`404` if no pending schedule has that id (already fired or cancelled).

See [Server Scheduling](../features/server-scheduling.md).

---

### DELETE `/api/v1/invitations/{id}`

Revoke a pending invitation.

**Auth:** JWT required. User must have the `manage-invitations` permission on `/invitations`.

No request body.

```json
{ "status": "revoked" }
```

---

### DELETE `/api/v1/users/{id}`

Delete a user account. Cannot delete self or the system agent user.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

No request body.

```json
{ "status": "deleted" }
```

---

### POST `/api/v1/users/{id}/unlockout`

Reset a user's login lockout, allowing them to log in immediately after
being locked out due to too many failed attempts.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

No request body.

```json
{ "status": "unlocked" }
```

---

### GET `/api/v1/users/{id}/sessions`

List a user's active sessions with their workstation identity — the
queryable half of concurrent-logon auditing. One item per unexpired
session, oldest first; `source_ip` is the effective client IP the
session was established from (null = unknown, e.g. sessions created
before the audit feature), `user_agent` is the client's User-Agent
string (null = none was sent), and `last_seen_at` is when the session
last showed activity (an authenticated request or WebSocket frame —
the clock the idle session timeout judges). Expired sessions are
excluded.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

No request body.

```json
{
  "items": [
    {
      "created_at": "2026-08-20 18:03:41",
      "expires_at": "2026-08-21 18:03:41.862671+00:00",
      "source_ip": "203.0.113.7",
      "user_agent": "klangk-cli/1.0",
      "last_seen_at": "2026-08-21 17:59:02.104881+00:00"
    }
  ]
}
```

---

### GET `/api/v1/acl/by-principal/group/{id}`

List all ACL entries granted to a specific group across all resources.

**Auth:** JWT required. User must have the `manage-acls` permission on `/acl`.

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

### GET `/api/v1/acl/by-principal/user/{id}`

List all ACL entries granted to a specific user across all resources.

**Auth:** JWT required. User must have the `manage-acls` permission on `/acl`.

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

### GET `/api/v1/acl/resource`

Get the ACL entries for a specific resource. Query param: `resource`
(e.g. `/workspaces/uuid`).

**Auth:** JWT required. User must have `manage-acls` permission on
`/acl`.

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

### GET `/api/v1/acl/tree`

Get a summary of the entire ACL tree across all resources.

**Auth:** JWT required. User must have the `manage-acls` permission on `/acl`.

No request body.

```json
[
  { "resource": "/workspaces/uuid", "ace_count": 3 },
  { "resource": "/groups/uuid", "ace_count": 1 }
]
```

---

### GET `/api/v1/groups`

List groups with pagination, filtering, and the paged envelope — the
one listing surface for every reader (pickers, share dialogs, the
admin Groups tab).

**Auth:** JWT required — any authenticated caller; writes on the tree
need `manage-groups`.

Query parameters:

- `page` (default 1), `page_size` (default 10, max 200)
- `sort` (`name` | `created`), `order` (`asc` | `desc`)
- `q` — substring filter on name (matched literally — `%` and `_`
  are characters, not wildcards)
- `source` — `manual` (hide the seeded per-workspace role groups) or
  `workspace-role` (show only them); omitted shows all rows only to
  callers holding `manage-groups` — every other authenticated caller
  gets the manual-only view (#3283)

No request body.

```json
{
  "groups": [
    {
      "id": "uuid",
      "name": "my-group",
      "description": null,
      "source": "manual",
      "created_at": "2025-01-01 12:00:00"
    }
  ],
  "page": 1,
  "page_size": 10,
  "total": 1
}
```

---

### GET `/api/v1/groups/{id}/members`

List members of a group (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/server/schedule`

List pending server stop/recycle schedules. Rows exist only while pending — fired or cancelled schedules are deleted.

**Auth:** JWT required. User must have the `manage-server-schedule` permission on `/server`.

No request body.

```json
{
  "schedules": [
    {
      "id": "uuid",
      "action": "recycle",
      "fire_at": "2026-08-24T21:00:00+00:00",
      "created_by": "uuid",
      "created_at": "2026-08-24T18:12:00+00:00"
    }
  ]
}
```

See [Server Scheduling](../features/server-scheduling.md).

---

### GET `/api/v1/invitations`

List all invitations (pending, accepted, and revoked).

**Auth:** JWT required. User must have the `manage-invitations` permission on `/invitations`.

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

### GET `/api/v1/users`

List all user accounts in the system.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

No request body.

```json
[
  {
    "id": "uuid",
    "email": "user@example.com",
    "verified": true,
    "provider": "local",
    "created_at": "2025-01-01 12:00:00",
    "disabled": false,
    "last_login_at": "2026-01-15T10:00:00+00:00",
    "last_activity_at": "2026-01-15T10:05:00+00:00",
    "groups": [{ "id": "uuid", "name": "admins" }]
  }
]
```

---

### GET `/api/v1/users/{id}/workspaces`

List workspaces owned by a user (admin). Used by the admin UI to show
what a delete-user will destroy. Returns the standard pagination
envelope.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

Query params: `limit` (1–200, default 100), `offset` (default 0).

```json
{
  "items": [
    /* workspace objects as in GET /api/v1/workspaces */
  ],
  "has_more": true,
  "next_offset": 100
}
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

Returns HTTP 302 redirect to `/#/oidc-complete?code=...` (a one-time code
redeemable via `POST /api/v1/auth/oidc/exchange`) or
`<cli_redirect>?code=...` on the CLI localhost callback.

---

### GET `/api/v1/auth/oidc/{provider_id}/login`

Initiate OIDC login. Redirects the user to the identity provider's
authorization endpoint.

**Auth:** None. Optional query param: `cli_redirect` (must be localhost URL).

Returns HTTP 302 redirect to OIDC IdP.

---

### POST `/api/v1/auth/verify`

Verify a user's email address using the token from the verification
email. Returns a session token on success. The token rides the request
body (never the URL — URLs land in proxy/server access logs); the token
is one-time: a replayed link, or one minted before an email change, is
rejected.

**Auth:** None. Body: `{"token": "<verification JWT from email link>"}`.

```json
{ "status": "verified", "access_token": "jwt-string" }
```

---

### POST `/api/v1/auth/oidc/exchange`

Redeem the one-time login code the OIDC callback redirected to
(`/#/oidc-complete?code=…` for the web flow, `?code=…` on the CLI's
localhost callback). Codes are single-use and expire after 60 seconds;
unknown, replayed, and expired codes all return 400.

**Auth:** None. Body: `{"code": "<one-time login code>"}`.

```json
{ "access_token": "jwt-string", "email": "user@example.com" }
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
are enabled, available OIDC providers, login banner text, auth mode,
and the branding / feature flags the pre-auth UI needs. An
**authenticated** caller additionally receives the deploy-wide netfilter
default + enabled flag (the egress perimeter is not exposed pre-auth)
and the deploy-level capability toggles `nix_available` /
`sudo_available` / `per_handle_home_available` (moved off the images
listing; whether the per-workspace nix mount can arm, whether the
deploy's sudo ceiling permits the per-workspace opt-in — sudo itself
is off unless the workspace's settings store `allow_sudo: true` — and
whether per-handle homes are permitted at all), plus any feature-declared frontend config keys and
`features_enable` when set.

**Auth:** None (public payload; authenticated callers get a few extra
fields).

No request body.

```json
{
  "registration_enabled": true,
  "invitations_enabled": true,
  "product_name": "Klangk",
  "login_banner_title": "",
  "login_banner": "",
  "login_banner_every_visit": false,
  "oidc_providers": [],
  "auth_modes": "both",
  "instance_id": "string",
  "allow_autostart": false,
  "browser_delegate_enabled": true,
  "default_per_handle_home": false,
  "default_classification_banner": "",
  "min_password_length": 8,
  "password_requirements": { "upper": 0, "lower": 0, "digit": 0, "special": 0 },
  "password_history_count": 0,
  "logo_url": "",
  "terms_url": "",
  "privacy_url": "",
  "aup_url": "",
  "support_url": "",
  "support_email": "",
  "nix_available": false,
  "sudo_available": true,
  "per_handle_home_available": false
}
```

The last two keys (like `netfilter_enabled`) appear only on the
authenticated payload.

`auth_modes` is a string — one of `password`, `oidc`, `both`, or `none`
(no-login single-user mode). The frontend and CLI branch on this value;
see [Auth Modes](../features/auth-modes.md).

---

### GET `/api/v1/events`

Time-correlated merged audit stream (#3251, SV-222439): one
newest-first page over all three audit tables — `audit_events`
(identity/privilege), `container_events` (lifecycle) and
`egress_consent` — merged by timestamp so an attack trail can be
replayed across components (a login, a workspace start, an
egress-consent decision, in order). Each item names its origin in
`source` (`audit` / `container` / `egress`) and embeds the full
origin row in `data`; the HMAC integrity tag (#3174) is
verification-internal and never ships. One merged row per origin
row — a consent row is named `egress.<decision>`, timestamped by its
`requested_at`, and attributed to its decider/revoker when a human
has acted on it. Query params: `limit` (1–200, default 50),
`offset`, `since` / `until` (inclusive epoch seconds), `actor`
(actor id or email substring; a consent row matches when the actor
is its decider **or** its revoker, while the summary row names the
revoker for a revoked verdict — the `data` blob carries both),
`workspace` (exact workspace id or a workspace-name substring), and
`event` (event-name substring). Substring filters match filter text
literally (`%` and `_` are characters, not wildcards).
`workspace_name` and `actor_email`
are resolved for display.

**Auth:** JWT required. User must have the `manage-events`
permission on `/events` (the same grant as the two per-table views).

No request body.

```json
{
  "items": [
    {
      "source": "container",
      "id": 1,
      "created_at": 1759200000.0,
      "event": "start",
      "actor_id": "uuid",
      "actor_type": "user",
      "actor_email": "user@example.com",
      "workspace_id": "uuid",
      "workspace_name": "my-ws",
      "data": {
        "id": 1,
        "workspace_id": "uuid",
        "event": "start",
        "actor_type": "user",
        "actor_id": "uuid",
        "cause": "api",
        "container_id": "abc123",
        "container_role": "workspace",
        "network_namespace": null,
        "created_at": 1759200000.0
      }
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

---

### GET `/api/v1/events/audit`

Paged identity/privilege audit history (#3205), newest first, from the
`audit_events` table: account create/update/delete, group and ACL
changes, workspace role assignments and transfers,
login/logout/failed-login, session revocation, and the data-level
file events (#3257) — `file.download` (a workspace archive export, a
per-file/directory download, or a text read via `/files/content`,
marked `via: content`), `file.upload` (a workspace archive
import), `file.write` (an upload or rename through the files API),
and `file.delete` (a delete through the files API) — each carrying
the path and byte size in `detail` (the size is omitted where
meaningless — rename, delete, directory downloads; an export's size
is a pre-flight estimate). Query params:
`limit` (1–200, default 50), `offset`, and optional `event`, `actor`
(matches actor id or email), and `target` (target id) substring filters
(matched literally — `%` and `_` are characters, not wildcards).
Each item carries the acting principal (`actor_id` / `actor_email` —
denormalized so attribution survives the actor's deletion), the target
(`target_type` / `target_id`), a read-only JSON `detail` blob
(action-specific context; never passwords or tokens), and the
request's `source_ip` / `user_agent` for correlation with client
activity. Since #3255 (SV-222447) every HTTP-minted row also records
the request's HTTP `method` (`GET`/`POST`/…) and `referer` header —
the method distinguishes a read from a state change on the same
endpoint, and the Referer shows which surface issued the request
(null for rows written before the field existed and for the
workstation-binding violation row, which records only the presenting
workstation pair; stored Referer values are truncated at 2048
characters). The HMAC integrity tag (#3174) is never sent on the
wire.

**Auth:** JWT required. User must have the `manage-events` permission on `/events`
(the same grant as the container history — the `/events` resource governs
both streams).

No request body.

```json
{
  "items": [
    {
      "id": 1,
      "event": "login",
      "actor_id": "uuid",
      "actor_email": "user@example.com",
      "target_type": "user",
      "target_id": "uuid",
      "detail": { "via": "password" },
      "source_ip": "203.0.113.7",
      "user_agent": "klangk-cli/1.0",
      "method": "POST",
      "referer": "https://klangk.example/login",
      "created_at": 1759200000.0
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

---

### GET `/api/v1/events/containers`

Paged container start/stop history (#2923), newest first, from the
`container_events` audit table (#2915) — the Containers stream of the
`/events` resource (the Audit stream is `GET /events/audit`, #3205).
Renamed from `GET /events` when the resource gained its second stream. Query params: `limit` (1–200,
default 50), `offset`, and either `workspace` (an exact workspace id or
a workspace-name substring, matched literally) or the legacy exact-match
`workspace_id` to
narrow to one workspace. Items carry the acting principal
(`actor_type` user/agent/system + `actor_id`), the `cause`
(api, idle_timeout, eviction, drain, …), the podman correlation ids
(`container_id`, `container_role` workspace/network-sidecar,
`network_namespace`), and the resolved `workspace_name` / `actor_email`
for display. The HMAC integrity tag (#3174) is never sent on the wire.

**Auth:** JWT required. User must have the `manage-events` permission on `/events`.

No request body.

```json
{
  "items": [
    {
      "id": 1,
      "workspace_id": "uuid",
      "workspace_name": "my-ws",
      "event": "start",
      "actor_type": "user",
      "actor_id": "uuid",
      "actor_email": "user@example.com",
      "cause": "api",
      "container_id": "abc123",
      "container_role": "workspace",
      "network_namespace": null,
      "created_at": 1759200000.0
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

---

### GET `/api/v1/images`

List available container images that can be used when creating or
editing workspaces. Deployment-level capability toggles (nix/sudo
availability) moved to the authenticated-only `/config` fields.

**Auth:** JWT required. User must have the `view-images` permission on
`/images` (seeded Allow for Authenticated — the deliberate,
ACL-editor-modifiable default).

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
    "/workspaces/uuid": ["view", "terminal", "files-view"]
  }
}
```

---

### GET `/api/v1/users/search`

Search for users by email or handle. Used for autocomplete when sharing
workspaces or adding group members.

**Auth:** JWT required. User must have the `search-users` permission on
`/users` (seeded Allow for Authenticated — distinct from
`manage-users`, so picker surfaces work for non-admins). Query param:
`q` (search string, min length 1). The needle is a literal prefix
match on the email or handle — `%` and `_` in `q` are matched as
characters, not as `LIKE` wildcards.

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

List every podman volume this klangk instance manages.

**Auth:** JWT required. User must have the `view-volumes` permission
on `/volumes` (seeded Allow for the `admins` group — the admin
Volumes tab's listing gate). Volumes are workspace-owned (#3153):
`workspace_id` is the owning workspace's id (from the podman label),
`workspace` its resolved name (null when the workspace row is gone),
and `workspaces` lists the workspace names whose extra mounts
reference the volume.

No request body.

**Query params (optional, server-side paging/sort/filter):**

| Param       | Type   | Default   | Constraints                          |
| ----------- | ------ | --------- | ------------------------------------ |
| `page`      | int    | `1`       | `>= 1` (clamped)                     |
| `page_size` | int    | `10`      | `1`–`200` (clamped)                  |
| `sort`      | string | `created` | `name` \| `created` (else `created`) |
| `order`     | string | `desc`    | `asc` \| `desc`                      |
| `q`         | string | (none)    | substring match, case-insensitive;   |
|             |        |           | `%` and `_` match literally          |

`q` matches the volume name, the owning workspace's name, or any
workspace name using the volume.

```json
{
  "volumes": [
    {
      "name": "my-volume",
      "created": "2025-01-01T12:00:00Z",
      "workspace_id": "<owning workspace id>",
      "workspace": "my-workspace",
      "workspaces": ["my-workspace"]
    }
  ],
  "page": 1,
  "page_size": 10,
  "total": 1
}
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
so `has_more`/`next_offset` reflect the filtered set. Filter text matches
literally — `%` and `_` in `q` are characters, not wildcards.

```json
[
  {
    "id": "uuid",
    "user_id": "uuid",
    "name": "my-workspace",
    "container_id": null,
    "num_ports": 5,
    "image": null,
    "service_command": null,
    "auto_start": false,
    "setup_state": "complete",
    "health_check": null,
    "mounts": null,
    "env": null,
    "allowed_domains": null,
    "rejected_domains": null,
    "settings": null,
    "egress_mode": "interactive",
    "per_handle_home": false,
    "classification_banner": null,
    "created_at": "2025-01-01 12:00:00"
  }
]
```

The same workspace-object shape is returned by `GET /workspaces/shared`,
`POST /workspaces`, `POST /workspaces/{id}/duplicate`, and
`POST /workspaces/import` (shared adds `owner_email`).

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
`?q=<substring>` (name substring, matched literally). Without params returns a bare list, with
params returns the `{ items, has_more, next_offset }` envelope.

Workspace objects use the same shape as
[GET /api/v1/workspaces](#get-apiv1workspaces), plus an `owner_email`
field.

---

### GET `/api/v1/workspaces/{id}/acl`

Get the resolved ACL entries for a workspace.

**Auth:** JWT required. User must have `share-advanced` permission on
`/workspaces/{id}`.

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

**Auth:** JWT required. User must have the `export-workspace` permission on the
workspace resource (`/workspaces/{id}`) — the owner's wildcard ACE and
the seeded `owners-<id>` role group both cover it.

No request body. Returns `StreamingResponse` (`.tar.gz` binary stream).
Headers: `Content-Disposition: attachment; filename="<name>.tar.gz"`,
`X-Estimated-Size: <bytes>`.

Each export writes a `file.download` audit row (#3257) with the actor,
workspace, archive name, and the size estimate as its byte size.

---

### GET `/api/v1/workspaces/{id}/files`

List files and directories inside the workspace container. Requires a
running container (returns 409 if stopped).

**Auth:** JWT required. User must have `files-view` permission on
`/workspaces/{id}`. Query param: `path` (absolute container path,
default `/`).

No request body.

```json
[
  {
    "name": "README.md",
    "path": "/home/klangk/README.md",
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

**Auth:** JWT required. User must have both `files-view` and `files-download`
permissions on `/workspaces/{id}`. Query param: `path` (absolute
container path).

Each read writes a `file.download` audit row (#3257) — the text read
sits behind the same `files-download` gate as the byte-stream download
and moves the same file data to the client; its detail carries
`"via": "content"` and the decoded content's byte count.

No request body.

```json
{ "path": "src/main.py", "content": "file contents as string" }
```

---

### GET `/api/v1/workspaces/{id}/files/download`

Download a file or directory from the workspace container. Single files
are streamed directly; directories are streamed as `.tar.gz` archives.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have both `files-view` and `files-download`
permissions on `/workspaces/{id}`. Query param: `path` (absolute container
path).

No request body. Returns a streamed `application/octet-stream` (single
file) or `application/gzip` (directory archive).

Each download writes a `file.download` audit row (#3257) with the
actor, workspace, path, and byte size (file rows only — a directory
stream's size is not observable up front).

---

### GET `/api/v1/workspaces/{id}/groups`

List groups that have been granted access to a workspace.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

No request body.

```json
[{ "id": "uuid", "name": "group-name" }]
```

---

### GET `/api/v1/workspaces/{id}/members`

List individual users who have been granted access to a workspace.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

No request body.

```json
[{ "id": "uuid", "email": "user@example.com", "handle": "user" }]
```

---

### GET `/api/v1/workspaces/{id}/roles`

List role groups for a workspace, their members, and each group's
effective permissions. Each workspace has four roles: `owners`,
`coders`, `collaborators`, `spectators`. `permissions` is read live
from the ACEs on `/workspaces/{id}` (not inherited from ancestors),
so post-seed ACL edits are reflected; a `*` grant expands to the
whole vocabulary, including the literal `*`.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

No request body.

```json
[
  {
    "role": "owners",
    "group_id": "uuid",
    "group_name": "owners-uuid",
    "members": [{ "id": "uuid", "email": "user@example.com" }],
    "permissions": ["*", "view", "terminal"]
  }
]
```

---

### PATCH `/api/v1/groups/{id}`

Update a group's name or description (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

```json
{ "name": "new-name", "description": "updated description" }
```

```json
{ "status": "updated" }
```

---

### PATCH `/api/v1/users/{id}`

Update a user's email, password, handle, or enabled state (admin). All
fields optional. `disabled` (see
[Dormant-account auto-disable](../features/authentication.md#dormant-account-auto-disable))
disables or re-enables the account — a disabled account's logins, token
refreshes, and authenticated requests fail with `403 Account disabled`,
and its live WebSocket connections are closed (4001 → client logout).
Admins cannot disable their own account, and the system agent cannot be
disabled.

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

```json
{
  "email": "new@example.com",
  "password": "newpass",
  "handle": "newhandle",
  "disabled": true,
  "must_change_password": false
}
```

`must_change_password` (#3172) sets or clears the forced-change flag on
a local-password account (`400` for OIDC-linked accounts — they have no
local password to change). Setting `password` implies
`must_change_password: true`; an explicit `must_change_password: false`
in the same request overrides that.

```json
{ "status": "updated" }
```

---

### PATCH `/api/v1/workspaces/{id}/roles`

Change a user's role in a workspace. Set `role` to `null` to remove the
user from all roles.

**Auth:** JWT required. User must have `share-workspace` **and** `share-advanced` permission on
`/workspaces/{id}`.

```json
{ "email": "user@example.com", "role": "coders" }
```

```json
{ "ok": true, "email": "user@example.com", "role": "coders" }
```

---

### PATCH `/api/v1/workspaces/{id}/settings`

Partial-merge update of the workspace's per-workspace `settings` bag —
each key in the body sets/replaces that override; `null` **deletes**
the key (reverting it to the deploy-wide default); keys not present
are left untouched. See the `settings` field on
[POST `/api/v1/workspaces`](#post-apiv1workspaces) for the known keys.

**Auth:** JWT required. User must have `edit-workspace` permission on `/workspaces/{id}`.

```json
{ "idle_timeout": 0 }
```

```json
{ "settings": { "idle_timeout": 0, "cpu_limit": 2.0 } }
```

An empty patch (`{}`) is rejected with `400`. Like a `PUT`, a changed
`idle_timeout` applies the next time the container starts.

---

### POST `/api/v1/groups`

Create a new group (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

```json
{ "name": "my-group", "description": "optional description" }
```

```json
{ "id": "uuid", "name": "my-group", "description": "optional description" }
```

---

### POST `/api/v1/groups/{id}/members`

Add a user to a group (admin).

**Auth:** JWT required. User must have `manage-groups` permission on `/groups`.

```json
{ "user_id": "uuid" }
```

```json
{ "status": "added" }
```

---

### POST `/api/v1/server/schedule`

Schedule a server stop or recycle for a future time. Provide `at` (absolute ISO-8601; a naive timestamp is interpreted as UTC) or `in_seconds` (positive relative delay; ignored when `at` is given); `action` is `stop` or `recycle`. The schedule persists in the DB across `klangkd` restarts; when it fires: a **stop** runs the graceful TERM/INT path and the process exits (code 0) — the service manager owns what happens next; a **recycle** runs the SIGHUP graceful restart in-process (listener and DB stay up) and never exits. In both, workspaces are drained gracefully and every connected client sees a live countdown.

**Auth:** JWT required. User must have the `manage-server-schedule` permission on `/server`.

```json
{ "action": "recycle", "at": "2026-08-24T23:00:00+02:00" }
```

Returns the created schedule:

```json
{
  "id": "uuid",
  "action": "recycle",
  "fire_at": "2026-08-24T21:00:00+00:00",
  "created_by": "uuid",
  "created_at": "2026-08-24T18:12:00+00:00"
}
```

`422` if `action` is invalid, `at` does not parse as ISO-8601, `in_seconds` is not a positive number, or neither `at` nor `in_seconds` is provided.

See [Server Scheduling](../features/server-scheduling.md).

---

### POST `/api/v1/invitations`

Send an invitation email to a new user.

**Auth:** JWT required. User must have the `manage-invitations` permission on `/invitations`.

```json
{ "email": "user@example.com" }
```

```json
{ "id": "uuid", "email": "user@example.com", "status": "pending" }
```

---

### POST `/api/v1/invitations/{id}/resend`

Resend an invitation email.

**Auth:** JWT required. User must have the `manage-invitations` permission on `/invitations`.

No request body.

```json
{ "status": "resent" }
```

---

### POST `/api/v1/users`

Create a new user account. By default the user is created verified with
the given password. Set `send_verification_email` to `true` to create
the user unverified and send a verification email so they can set their
own password (the `password` field is ignored in this case).

**Auth:** JWT required. User must have the `manage-users` permission on `/users`.

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
Clears `must_change_password` atomically with the new hash (#3172) —
this is the **only** endpoint a flagged session may call.

**Auth:** JWT required.

```json
{ "current_password": "oldpass", "new_password": "newpass" }
```

```json
{ "status": "updated" }
```

---

### POST `/api/v1/auth/change-expired-password`

Replace an expired password (maximum password age reached, #3177)
and auto-login. Takes the identifier (email or
handle), the current — expired — password as the ownership proof, and
the new password. Lockout-accounted like login; rejected with `400`
when the password has **not** expired (so it cannot serve as a general
change-password bypass). Refused with `403` when password login is
disabled (`oidc`-only mode), like `/auth/login`. Local password
accounts only — OIDC logins are unaffected by password expiry. An
expired password also fails the next authenticated request and
WebSocket connect on any already-minted token (same posture as a
disabled account), so expiry takes effect mid-session, not just at
the next refresh.

**Auth:** None.

```json
{
  "identifier": "user@example.com",
  "current_password": "expiredpass",
  "new_password": "newpass1"
}
```

```json
{ "access_token": "jwt-string", "token_type": "bearer" }
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
{
  "access_token": "jwt-string",
  "token_type": "bearer",
  "must_change_password": false
}
```

`must_change_password` (#3172) is `true` when the password was set by an
admin and has not been changed since. Such a session may only call
`POST /api/v1/auth/change-password` (and refresh); every other
authenticated request returns `403 Password change required` and new
WebSocket connections close with `4004`.

---

### POST `/api/v1/auth/local`

No-login single-user mode: mint a JWT for the seeded default user with no
credentials. Only available when `KLANGKD_AUTH_MODES=none`; returns **403**
otherwise. See [Auth Modes](../features/auth-modes.md).

**Auth:** None. Reachable only from loopback (the `KLANGKD_LISTEN` bind plus an
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

**Auth:** none required — logout is idempotent. An absent,
expired, revoked, or invalid token still returns 200 (the token being
unusable is logout's desired end state); the token is blocklisted when
one is presented. `oidc_logout_url` is returned only when a valid token
resolves to a live user.

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
blocklisted. With session binding armed
(`KLANGKD_SESSION_WORKSTATION_BINDING`, #3194), a token presented from a different
workstation than it was issued to is refused with `401` and its
session revoked.

**Auth:** JWT required.

No request body.

```json
{
  "access_token": "new-jwt-string",
  "token_type": "bearer",
  "must_change_password": false
}
```

`must_change_password` (#3172) reflects the live flag at refresh time
(the same semantics as the login response above).

---

### POST `/api/v1/auth/register`

Create a new user account. A verification email is sent unless running
in test mode. Can be disabled via `KLANGKD_DISABLE_REGISTRATION`.

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
Returns **403** when the deploy disabled the bridge
(`KLANGKD_BROWSER_DELEGATE_ENABLED=false`), when the `browser_id`
is unknown, or when it is not registered against the caller's workspace.

```json
{ "action": "navigate", "browser_id": "string" }
```

Returns forwarded result from the target browser tab (arbitrary JSON).

---

### POST `/api/v1/browser-delegate/stream`

Streaming variant of browser-delegate. Returns results as newline-
delimited JSON chunks.

**Auth:** Workspace JWT required + proxy IP ACL (container traffic only).
Returns **403** when the deploy disabled the bridge
(`KLANGKD_BROWSER_DELEGATE_ENABLED=false`), when the `browser_id`
is unknown, or when it is not registered against the caller's workspace.

```json
{ "action": "string", "browser_id": "string" }
```

Returns `StreamingResponse` (`application/x-ndjson`).

---

### POST `/api/v1/volumes`

Create a new podman volume owned by a workspace (#3153): labeled
with the instance and the workspace's id, never a user. The volume is
mountable by that workspace alone — volumes cannot be shared between
workspaces. The admin Volumes tab offers no create surface; this
endpoint serves the CLI's volume commands.

**Auth:** JWT required. User must have the `manage-volumes` permission
on `/volumes` (seeded Allow for the `admins` group).

**Quota:** when `KLANGKD_VOLUME_QUOTA_PER_WORKSPACE` is set
(nonzero), a create that would take the workspace past the cap is
refused with `429` and a "delete a volume first" message naming the
setting; the count is the workspace's instance-managed volumes, and a
per-workspace lock makes the cap exact under concurrent creates. The
same cap also gates the workspace-start auto-create of mounted named
volumes. `0` (the default) = unlimited.

`name` must be podman-safe: start with an alphanumeric
character, continue with `a-zA-Z0-9_.-` only, and be at most 64
characters; violations return HTTP 422.

```json
{ "name": "my-volume", "workspace": "<workspace-id>" }
```

`workspace` is the owning workspace's id; an unknown id returns `404`.

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
  "env": { "MY_VAR": "value" },
  "per_handle_home": true,
  "classification_banner": "CUI",
  "settings": { "idle_timeout": 0 }
}
```

All fields except `name` are optional. `per_handle_home` selects the
[home layout](../features/workspaces.md#home-directory-layout): `true`
gives each member a private `/home/<handle>`, `false` (the server
default) shares one `/home/klangk`. Omit it to inherit the server
default. The deploy-wide `KLANGKD_PER_HANDLE_HOME` is a **ceiling**:
while it is `false`, a `per_handle_home: true` is stored but inert —
every workspace gets the shared home (clamped at the next
connect/start, no 400 — the same shape `allow_sudo` got in #3047).

A `mounts` source with no `/` that doesn't start with `.` is a named
volume and must be podman-safe: start with an alphanumeric
character, continue with `a-zA-Z0-9_.-` only, and be at most 64
characters — the same rule as the volumes API. Violations
return HTTP 400; bind-mount sources (absolute paths, `.`-prefixed)
keep the protected-path / allowed-root checks.

`settings` is a bag of per-workspace behavioral overrides. Known
keys (unknown keys are rejected with `400`):

| Key              | Type    | Meaning                                                                                                   |
| ---------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| `idle_timeout`   | int (s) | Idle timeout override; `0` = never idle out, unset = deploy default. Applies at the next container start. |
| `bridge_timeout` | int (s) | Browser-delegate stream bridge timeout.                                                                   |
| `cpu_limit`      | float   | `--cpus` limit (e.g. `2.0`).                                                                              |
| `memory_limit`   | string  | `--memory` limit (e.g. `"4g"`, `"512m"`).                                                                 |
| `pids_limit`     | int     | `--pids-limit` (e.g. `512`).                                                                              |
| `tmp_size`       | string  | `/tmp` tmpfs size (e.g. `"4g"`).                                                                          |
| `nix`            | bool    | Mount a per-workspace `/nix`.                                                                             |
| `allow_sudo`     | bool    | Per-workspace sudo opt-in; absent = off (deploy `KLANGKD_ALLOW_SUDO` is a ceiling).                       |

`classification_banner` is the workspace's classification marking,
rendered as the persistent banner on the workspace page. Free
text, one line. Omitted/empty = inherit the deploy-wide default
(`KLANGKD_CLASSIFICATION_BANNER`), resolved at display time; when
neither is set, no banner is rendered.

Returns the created workspace record (the full workspace-object shape
shown under [GET /api/v1/workspaces](#get-apiv1workspaces)).

---

### POST `/api/v1/workspaces/{id}/transfer`

Transfer workspace ownership to another user. The new owner is added to
the workspace's `owners` role; connected clients of both users get a
workspaces-changed refresh. The caller must be a workspace admin (the
owner's wildcard ACE covers it).

**Auth:** JWT required. User must have `transfer-workspace` permission
on `/workspaces/{id}`.

```json
{ "email": "newowner@example.com" }
```

Returns the updated workspace record. `404` if the target user does not
exist; `409` on transfer conflicts (e.g. transferring to a member whose
role would collide).

---

### POST `/api/v1/workspaces/import`

Create a new workspace from a previously exported `.tar.gz` archive.
Environment variables are sanitized during import.

Each import writes a `file.upload` audit row (#3257) with the actor,
the created workspace, the upload's filename, and the streamed byte
count.

**Auth:** JWT required. Multipart form upload: `file` (`.tar.gz` archive),
optional `name` form field.

Returns the created workspace record (the full workspace-object shape
shown under [GET /api/v1/workspaces](#get-apiv1workspaces)).

---

### POST `/api/v1/workspaces/{id}/duplicate`

Clone an existing workspace's configuration into a new workspace.

**Auth:** JWT required. User must have `duplicate-workspace` permission
on `/workspaces/{id}`.

```json
{ "name": "cloned-workspace" }
```

Returns the created workspace record (the full workspace-object shape
shown under [GET /api/v1/workspaces](#get-apiv1workspaces)).

---

### POST `/api/v1/workspaces/{id}/files/rename`

Rename or move a file or directory inside the workspace container.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have both `files` and `files-write`
permissions on `/workspaces/{id}`.

```json
{ "old_path": "/home/klangk/old.py", "new_path": "/home/klangk/new.py" }
```

```json
{ "path": "/home/klangk/new.py", "status": "renamed" }
```

Each rename writes a `file.write` audit row (#3257) carrying the new
path and the old one in `from`.

---

### POST `/api/v1/workspaces/{id}/files/upload`

Upload a file into the workspace container. Default 500 MB limit.
Requires a running container (returns 409 if stopped).

**Auth:** JWT required. User must have both `files` and `files-write`
permissions on `/workspaces/{id}`. Multipart form: `file` (upload),
optional `path` query param (absolute container path).

```json
{ "path": "/home/klangk/uploads/file.txt", "status": "uploaded" }
```

Each upload writes a `file.write` audit row (#3257) with the actor,
workspace, saved path, and byte count.

---

### POST `/api/v1/workspaces/{id}/groups`

Grant a group access to a workspace.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

```json
{ "group_id": "uuid" }
```

```json
{ "status": "shared", "group_id": "uuid", "name": "group-name" }
```

---

### POST `/api/v1/workspaces/{id}/members`

Grant a user access to a workspace. The user receives `view`,
`monitor-workspace`,
`terminal`, `files`, `files-download`, and `files-write` permissions
(a direct user share — the role-bucket sharing UI uses
`POST /workspaces/{id}/roles/{role}` instead).

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

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

### POST `/api/v1/workspaces/{id}/stop`

Stop a running workspace container. Emits terminal status/death frames,
stops and removes the container, and closes active terminal sessions.
Idempotent — returns 200 even if the container is already stopped.

**Auth:** JWT required. User must have `terminal` permission on
`/workspaces/{id}`.

No request body.

```json
{ "status": "stopped" }
```

---

### POST `/api/v1/workspaces/{id}/start`

Start a stopped workspace container. Creates a fresh container from the
workspace config (the service command re-fires via the create choke
point). No-op if already running.

**Auth:** JWT required. User must have `terminal` permission on
`/workspaces/{id}`.

No request body.

**Response when started:**

```json
{ "status": "started" }
```

**Response when already running:**

```json
{ "status": "already_running" }
```

---

### GET `/api/v1/workspaces/{id}/status`

Return the container status for a workspace.

**Auth:** JWT required. User must have `monitor-workspace` permission on
`/workspaces/{id}` (the dedicated status-observation permission,
granted alongside `terminal` by every share path).

No request body.

**Response when running:**

```json
{
  "running": true,
  "container_id": "abc123...",
  "health": null,
  "health_message": null,
  "idle_seconds": 42.5,
  "idle_timeout": 3600,
  "ports": [9000, 9001],
  "restart": {
    "state": "recovering",
    "attempts": 1,
    "last_cause": "OOM-killed at 8g memory limit (exit code 137)"
  }
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
  "ports": [],
  "restart": {
    "state": "crash-loop",
    "attempts": 5,
    "last_cause": "main process exited with code 1",
    "gave_up_at": "2026-02-08T12:00:00+00:00"
  }
}
```

The `restart` field is `null` when the workspace has no crash-recovery
history (the common case); otherwise `state` is one of `dead` (died,
restart disabled or not yet scheduled), `backing-off` (a restart is
scheduled — `next_attempt_at` tells when), `recovering` (a restarted
container is still inside its 10-minute stability window), or
`crash-loop` (the bounded retry budget was exhausted — `gave_up_at`
tells when). See [Crash recovery](../features/crash-recovery.md).

The `health` field is the check status (`"healthy"`, `"unhealthy"`, or
`null` when no check is configured or no container is running). When
unhealthy, `health_message` carries a bounded tail of the check's
stderr/stdout explaining _why_ it failed (`null` otherwise) — so a
failing check isn't a black box.

---

### POST `/api/v1/workspaces/{id}/roles/{role}`

Add a user to a workspace role. Valid roles: `owners`, `coders`,
`collaborators`, `spectators`.

**Auth:** JWT required. User must have `share-workspace` **and** `share-advanced` permission on
`/workspaces/{id}`.

```json
{ "email": "user@example.com" }
```

```json
{ "ok": true }
```

---

### PUT `/api/v1/acl/resource`

Replace all ACL entries for a specific resource. Query param: `resource`.

**Auth:** JWT required. User must have `manage-acls` permission on
`/acl`. When the target is an individual workspace (`/workspaces/{id}`),
the user must additionally hold `share-advanced` on that workspace.

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

**Auth:** JWT required. User must have `edit-workspace` permission on
`/workspaces/{id}`.

```json
{
  "name": "renamed",
  "image": "klangk-workspace:latest",
  "service_command": "/bin/bash",
  "mounts": ["vol:/data"],
  "env": { "KEY": "VALUE" },
  "per_handle_home": false,
  "classification_banner": "",
  "settings": { "idle_timeout": 600 }
}
```

`per_handle_home` may be flipped here too; the new layout applies from
the workspace's next connect/start (open terminals keep their layout
until they end). While the deploy ceiling (`KLANGKD_PER_HANDLE_HOME`)
is `false`, a stored `true` is inert — the workspace resolves to the
shared home on every connect/start (no DB rewrite, no 400).

`mounts` named-volume sources are subject to the same podman-safe
rule as on create; violations return HTTP 400.

`settings` is a **full replace** of the
[settings bag](#post-apiv1workspaces) — keys absent from the request are
reverted to the deploy-wide default, and an explicit `null` clears the
whole bag. Use `PATCH /api/v1/workspaces/{id}/settings` to merge
individual keys instead.

`classification_banner` replaces the workspace's marking
outright; an empty value clears the override back to the deploy-wide
default (`KLANGKD_CLASSIFICATION_BANNER`). The banner is display-only —
no container restart is needed and the web UI updates it live.

```json
{ "status": "updated" }
```

---

### PUT `/api/v1/workspaces/{id}/acl`

Replace all ACL entries for a workspace.

**Auth:** JWT required. User must have `share-advanced` permission on
`/workspaces/{id}` — `share-workspace` no longer suffices.

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

### DELETE `/api/v1/volumes/{name}`

Delete an instance-managed podman volume. Any holder of the
permission may delete any instance volume (the surface is
admin-only by seed; the creator label is provenance, not an access
filter).

**Auth:** JWT required. User must have the `manage-volumes` permission
on `/volumes` (seeded Allow for the `admins` group).

`{name}` in the path must satisfy the same podman-safe rule as on
create; violations return HTTP 422.

No request body.

```json
{ "status": "deleted" }
```

---

### DELETE `/api/v1/workspaces/{id}`

Delete a workspace and stop its container.

**Auth:** JWT required. User must have `delete-workspace` permission on
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

**Auth:** JWT required. User must have both `files` and `files-write`
permissions on `/workspaces/{id}`.

Each delete writes a `file.delete` audit row (#3257) with the actor,
workspace, and the removed path.

No request body.

```json
{ "path": "src/old.py", "status": "deleted" }
```

---

### DELETE `/api/v1/workspaces/{id}/groups/{group_id}`

Revoke a group's access to a workspace.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/workspaces/{id}/members/{member_id}`

Revoke a user's access to a workspace.

**Auth:** JWT required. User must have `share-workspace` permission on `/workspaces/{id}`.

No request body.

```json
{ "status": "removed" }
```

---

### DELETE `/api/v1/workspaces/{id}/roles/{role}/{member_id}`

Remove a user from a workspace role.

**Auth:** JWT required. User must have `share-workspace` **and** `share-advanced` permission on
`/workspaces/{id}`.

No request body.

```json
{ "ok": true }
```

---

### GET `/audit`

Container-audit status (#3154, security finding):
`write_failures` counts every `container_events` write that failed
(best-effort paths included), `identity_write_failures` counts the
same for the `audit_events` identity/privilege table (#3206), and
`fail_closed` mirrors `KLANGKD_AUDIT_FAIL_CLOSED` so an assessor can
verify the mode. The counters are in-memory and **since process
start** — a restart (including a crash-restart) zeroes them, so `0`
means "no failures this process", not "no failures ever".

Deliberately public like `/health` (a deliberate, documented decision:
the values carry no user data and an assessor must be able to probe the
mode unauthenticated; the recon signal — "audit writes are failing
now" — was judged an acceptable trade for verifiability).

**Auth:** None.

No request body.

```json
{ "write_failures": 0, "identity_write_failures": 0, "fail_closed": false }
```

---

### GET `/empty`

Returns an empty page. Used as a lightweight OAuth callback landing URL
so the popup doesn't need to boot the Flutter SPA.

**Auth:** None.

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
terminal I/O, workspace status updates, and browser
delegate events.

**Auth:** JWT via the handshake's `Sec-WebSocket-Protocol` header
(`bearer, <jwt>` — the server echoes `bearer`; #3201).

Close codes: 4001 (missing/invalid token), 4002 (expired token), 4004
(password change required, #3172 — the account must change its password
before opening any WebSocket connection).

---

### WebSocket `/ws/consent-decider`

Registers a live egress-consent decider for its connection lifetime
— the interactive half of egress filtering. While a
decider is connected (and pinging inside
`KLANGKD_CONSENT_DECIDER_TIMEOUT`), held egress requests are offered to
it for accept/deny; the `klangk consent-decide` command drives this
socket. Requires the `egress-consent` permission on the workspace
(see [Egress Filtering](../features/egress-filtering.md)). Deciders are
strictly workspace-scoped: the `workspace` query param is
required — a handshake without it is refused.

**Auth:** JWT via the handshake's `Sec-WebSocket-Protocol` header
(`bearer, <jwt>` — the server echoes `bearer`; #3201). Query param:
`workspace` (the workspace id).

Close codes: 4001 (missing/invalid token), 4002 (expired token), 4003
(forbidden — missing `egress-consent` or wrong scope), 4004 (password
change required, #3172).

---

### WebSocket `/ws/egress-sidecar`

The network sidecar's blocked-egress event channel: the sidecar
sends blocked-egress events here and receives verdicts (hold-and-prompt
or static deny). Also carries revoke acks. Container-side only —
authenticated with the workspace JWT validated by the egress listener's
`forward_auth`.

**Auth:** Workspace JWT.

---

## LLM Proxy Endpoints

Served under `/llm-proxy/` (no `/api/v1` prefix) — on the egress
listener, gated by the proxy's workspace-JWT `forward_auth` + container
IP ACL, so only workspace containers can reach them. The backend
re-validates the workspace JWT itself: user login tokens and
anonymous requests are rejected with `401` even on the backend port.
See [LLM Proxy](../architecture/llm-proxy.md).

**Auth:** Workspace JWT.

### GET `/llm-proxy/models`

List the models the LLM router knows about. In passthrough mode queries
the upstream's `/models` endpoint (dynamic discovery); in router mode
returns the configured model names. Response matches the OpenAI
`/v1/models` shape.

### POST `/llm-proxy/chat/completions`

Proxy a chat-completions request to the configured provider(s) — standard
OpenAI `/v1/chat/completions` body; in router mode the `model` field
selects the provider. Streaming (`stream: true`) forwards the upstream
SSE stream. `503` when no LLM router is configured.

---

## Test-Only Endpoints

Available only when `KLANGKD_TEST_MODE` is set. No auth required.

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

- `KLANGKD_LOGIN_LOCKOUT_FAILURES` (default `5`) —
  attempts before lockout (0 = disabled)
- `KLANGKD_LOGIN_LOCKOUT_DURATION` (default `900`) —
  lockout period in seconds
- `KLANGKD_LOGIN_LOCKOUT_WINDOW` (default `300`) —
  attempt counting window in seconds

### Email Rate Limiting

- Verification resend: 60s per email (in-memory)
- Password reset: 60s per email (in-memory)
