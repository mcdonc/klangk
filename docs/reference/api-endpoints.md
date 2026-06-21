# API Endpoints

All HTTP and WebSocket endpoints with their authentication, authorization, and security constraints.

**Auth types**:

- **None** — public, no credentials required
- **JWT** — `Authorization: Bearer <access_token>` (user session)
- **ACL** — JWT + permission check on a resource (e.g. `view` on `/workspaces/{id}`)
- **Workspace JWT** — `Authorization: Bearer <workspace_token>` (container→host)

## Health & Config

| Method | Path          | Auth | Notes                                                            |
| ------ | ------------- | ---- | ---------------------------------------------------------------- |
| GET    | `/health`     | None | Readiness check                                                  |
| GET    | `/version`    | None | Build version, commit, timestamp                                 |
| GET    | `/api/config` | None | Registration status, OIDC providers, login banner, plugin config |

## Authentication

### Public

| Method | Path                                | Auth | Notes                                      |
| ------ | ----------------------------------- | ---- | ------------------------------------------ |
| POST   | `/auth/register`                    | None | Disabled via `KLANGK_DISABLE_REGISTRATION` |
| POST   | `/auth/login`                       | None | Rate-limited (see below)                   |
| GET    | `/auth/verify`                      | None | Email verification via query param token   |
| POST   | `/auth/resend-verification`         | None | 60s rate limit per email                   |
| POST   | `/auth/forgot-password`             | None | 60s rate limit per email                   |
| POST   | `/auth/reset-password`              | None | Token from email                           |
| POST   | `/auth/accept-invite`               | None | Invitation token in body                   |
| GET    | `/auth/oidc/{provider_id}/login`    | None | Redirect to IdP                            |
| GET    | `/auth/oidc/{provider_id}/callback` | None | IdP callback (validates state cookie)      |

### Authenticated

| Method | Path                    | Auth | Notes                                                       |
| ------ | ----------------------- | ---- | ----------------------------------------------------------- |
| POST   | `/auth/refresh`         | JWT  | Exchange token for new one (idempotent, blocklists old JTI) |
| GET    | `/auth/me`              | JWT  | Current user profile                                        |
| POST   | `/auth/change-password` | JWT  | Requires current password                                   |
| POST   | `/auth/change-email`    | JWT  | Requires password; marks account unverified                 |
| POST   | `/auth/change-handle`   | JWT  | Requires password                                           |
| POST   | `/auth/logout`          | JWT  | Blocklists token JTI                                        |

### Internal

| Method | Path                           | Auth          | Notes                                                        |
| ------ | ------------------------------ | ------------- | ------------------------------------------------------------ |
| GET    | `/auth/verify-workspace-token` | Workspace JWT | Used by nginx `auth_request`; returns `X-Token-Error` header |

## Workspaces

### List & Create

| Method | Path                 | Auth | Notes                                                      |
| ------ | -------------------- | ---- | ---------------------------------------------------------- |
| GET    | `/workspaces`        | JWT  | User's own workspaces                                      |
| GET    | `/workspaces/shared` | JWT  | Workspaces shared with user                                |
| POST   | `/workspaces`        | JWT  | Create workspace; owner gets full ACL; creates role groups |

### Modify

| Method | Path                         | Auth          | Notes                                    |
| ------ | ---------------------------- | ------------- | ---------------------------------------- |
| PUT    | `/workspaces/{id}`           | ACL: edit     | Update name, image, command, mounts, env |
| DELETE | `/workspaces/{id}`           | ACL: delete   | Delete workspace and stop container      |
| POST   | `/workspaces/{id}/duplicate` | ACL: create   | Clone workspace config                   |
| POST   | `/workspaces/{id}/restart`   | ACL: terminal | Stop and remove container                |

### Export & Import

| Method | Path                      | Auth       | Notes                                                  |
| ------ | ------------------------- | ---------- | ------------------------------------------------------ |
| GET    | `/workspaces/{id}/export` | ACL: admin | Stream .tar.gz archive; admin can export any workspace |
| POST   | `/workspaces/import`      | JWT        | Create from .tar.gz; sanitizes env vars                |

### Sharing & Members

| Method | Path                                   | Auth       | Notes                                         |
| ------ | -------------------------------------- | ---------- | --------------------------------------------- |
| GET    | `/workspaces/{id}/members`             | ACL: share | List members with access                      |
| POST   | `/workspaces/{id}/members`             | ACL: share | Add user (grants view, terminal, files, chat) |
| DELETE | `/workspaces/{id}/members/{member_id}` | ACL: share | Remove user                                   |

### Roles

| Method | Path                                        | Auth       | Notes              |
| ------ | ------------------------------------------- | ---------- | ------------------ |
| GET    | `/workspaces/{id}/roles`                    | ACL: share | List role groups   |
| POST   | `/workspaces/{id}/roles/{role}`             | ACL: share | Add user to role   |
| DELETE | `/workspaces/{id}/roles/{role}/{member_id}` | ACL: share | Remove from role   |
| PATCH  | `/workspaces/{id}/roles`                    | ACL: share | Atomic role change |

### Groups & ACL

| Method | Path                                 | Auth       | Notes                    |
| ------ | ------------------------------------ | ---------- | ------------------------ |
| GET    | `/workspaces/{id}/groups`            | ACL: share | List groups with access  |
| POST   | `/workspaces/{id}/groups`            | ACL: share | Share with group         |
| DELETE | `/workspaces/{id}/groups/{group_id}` | ACL: share | Revoke group access      |
| GET    | `/workspaces/{id}/acl`               | ACL: share | Get resolved ACL entries |
| PUT    | `/workspaces/{id}/acl`               | ACL: share | Replace all ACL entries  |

## Files

| Method | Path                              | Auth       | Notes                         |
| ------ | --------------------------------- | ---------- | ----------------------------- |
| GET    | `/workspaces/{id}/files`          | ACL: files | List directory                |
| GET    | `/workspaces/{id}/files/content`  | ACL: files | Read file                     |
| DELETE | `/workspaces/{id}/files`          | ACL: files | Delete file or directory      |
| POST   | `/workspaces/{id}/files/rename`   | ACL: files | Rename                        |
| GET    | `/workspaces/{id}/files/download` | ACL: files | Download as .zip              |
| POST   | `/workspaces/{id}/files/upload`   | ACL: files | Upload (default 500 MB limit) |

## Volumes

| Method | Path              | Auth | Notes                          |
| ------ | ----------------- | ---- | ------------------------------ |
| GET    | `/volumes`        | JWT  | List user's volumes            |
| POST   | `/volumes`        | JWT  | Create labeled volume          |
| DELETE | `/volumes/{name}` | JWT  | Delete (checks user ownership) |

## Groups

### User APIs

| Method | Path                             | Auth                | Notes                   |
| ------ | -------------------------------- | ------------------- | ----------------------- |
| GET    | `/groups`                        | JWT                 | List all groups         |
| POST   | `/groups`                        | ACL: create         | Create group            |
| PATCH  | `/groups/{id}`                   | ACL: edit           | Update name/description |
| DELETE | `/groups/{id}`                   | ACL: delete         | Delete group            |
| GET    | `/groups/{id}/members`           | ACL: view           | List members            |
| POST   | `/groups/{id}/members`           | ACL: manage_members | Add member              |
| DELETE | `/groups/{id}/members/{user_id}` | ACL: manage_members | Remove member           |

### Admin APIs

| Method | Path                                   | Auth       | Notes           |
| ------ | -------------------------------------- | ---------- | --------------- |
| GET    | `/admin/groups`                        | ACL: admin | List all groups |
| POST   | `/admin/groups`                        | ACL: admin | Create group    |
| PATCH  | `/admin/groups/{id}`                   | ACL: admin | Update group    |
| DELETE | `/admin/groups/{id}`                   | ACL: admin | Delete group    |
| GET    | `/admin/groups/{id}/members`           | ACL: admin | List members    |
| POST   | `/admin/groups/{id}/members`           | ACL: admin | Add member      |
| DELETE | `/admin/groups/{id}/members/{user_id}` | ACL: admin | Remove member   |

## Admin — Users & Invitations

| Method | Path                             | Auth       | Notes                                     |
| ------ | -------------------------------- | ---------- | ----------------------------------------- |
| GET    | `/admin/users`                   | ACL: admin | List all users                            |
| POST   | `/admin/users`                   | ACL: admin | Create verified user                      |
| DELETE | `/admin/users/{id}`              | ACL: admin | Delete user (cannot delete self or agent) |
| PATCH  | `/admin/users/{id}`              | ACL: admin | Update email, password, handle            |
| POST   | `/admin/invitations`             | ACL: admin | Send invitation email                     |
| GET    | `/admin/invitations`             | ACL: admin | List invitations                          |
| DELETE | `/admin/invitations/{id}`        | ACL: admin | Revoke invitation                         |
| POST   | `/admin/invitations/{id}/resend` | ACL: admin | Resend invitation email                   |

## Admin — ACL

| Method | Path                                 | Auth       | Notes                      |
| ------ | ------------------------------------ | ---------- | -------------------------- |
| GET    | `/admin/acl/tree`                    | ACL: admin | ACL tree summary           |
| GET    | `/admin/acl/by-principal/user/{id}`  | ACL: admin | Entries granted to user    |
| GET    | `/admin/acl/by-principal/group/{id}` | ACL: admin | Entries granted to group   |
| GET    | `/admin/acl/resource`                | ACL: admin | ACL for a resource         |
| PUT    | `/admin/acl/resource`                | ACL: admin | Replace ACL for a resource |

## Container→Host Endpoints

These are restricted at the nginx level to container traffic (IP ACL) and validated via workspace JWT (`auth_request`). The backend also validates the workspace JWT as defense-in-depth.

| Method | Path                               | Auth                   | Notes                            |
| ------ | ---------------------------------- | ---------------------- | -------------------------------- |
| POST   | `/api/browser-delegate`            | Workspace JWT + IP ACL | Relay request to browser tab     |
| POST   | `/api/browser-delegate/stream`     | Workspace JWT + IP ACL | Streaming variant (NDJSON)       |
| POST   | `/api/workspace/post-chat-message` | Workspace JWT + IP ACL | Post chat message from container |

The LLM proxy (`/llm-proxy/...`) is also gated by workspace JWT + IP ACL but is handled entirely by nginx (proxies to `$KLANGK_LLM_BASE_URL` with injected API key).

## Utility

| Method | Path                  | Auth | Notes                           |
| ------ | --------------------- | ---- | ------------------------------- |
| GET    | `/users/search`       | JWT  | Search users by email/handle    |
| GET    | `/images`             | JWT  | List available container images |
| GET    | `/api/my-permissions` | JWT  | User's effective permissions    |

## WebSocket

| Path  | Auth                          | Notes                                               |
| ----- | ----------------------------- | --------------------------------------------------- |
| `/ws` | JWT via `?token=` query param | Close codes: 4001 (missing/invalid), 4002 (expired) |

## Test-Only Endpoints

Available only when `KLANGK_TEST_MODE` is set.

| Method | Path                             | Auth | Notes                      |
| ------ | -------------------------------- | ---- | -------------------------- |
| GET    | `/api/test/idle-timeout`         | None | Get idle timeout           |
| POST   | `/api/test/set-idle-timeout`     | None | Set idle timeout           |
| GET    | `/api/test/workspace-token/{id}` | None | Generate workspace JWT     |
| GET    | `/api/test/browsers/{id}`        | None | List browser registrations |

## Rate Limiting

### Login Brute-Force Protection

Disabled by default. Configure via environment variables:

| Variable                        | Default | Description                            |
| ------------------------------- | ------- | -------------------------------------- |
| `KLANGK_LOGIN_LOCKOUT_FAILURES` | `0`     | Attempts before lockout (0 = disabled) |
| `KLANGK_LOGIN_LOCKOUT_DURATION` | `900`   | Lockout period in seconds              |
| `KLANGK_LOGIN_LOCKOUT_WINDOW`   | `300`   | Attempt counting window in seconds     |

### Email Rate Limiting

- Verification resend: 60s per email (in-memory)
- Password reset: 60s per email (in-memory)
