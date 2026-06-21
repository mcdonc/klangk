# API Endpoints

All HTTP and WebSocket endpoints with their authentication, authorization, and security constraints.

**Auth types**:

- **None** — public, no credentials required
- **JWT** — `Authorization: Bearer <access_token>` (user session)
- **ACL** — JWT + permission check on a resource (e.g. `view` on `/workspaces/{id}`)
- **Workspace JWT** — `Authorization: Bearer <workspace_token>` (container→host)

## Health & Config

| Method | Path              | Auth | Notes                                                            |
| ------ | ----------------- | ---- | ---------------------------------------------------------------- |
| GET    | `/health`         | None | Readiness check                                                  |
| GET    | `/api/v1/version` | None | Build version, commit, timestamp                                 |
| GET    | `/api/v1/config`  | None | Registration status, OIDC providers, login banner, plugin config |

## Authentication

### Public

| Method | Path                                       | Auth | Notes                                      |
| ------ | ------------------------------------------ | ---- | ------------------------------------------ |
| POST   | `/api/v1/auth/register`                    | None | Disabled via `KLANGK_DISABLE_REGISTRATION` |
| POST   | `/api/v1/auth/login`                       | None | Rate-limited (see below)                   |
| GET    | `/api/v1/auth/verify`                      | None | Email verification via query param token   |
| POST   | `/api/v1/auth/resend-verification`         | None | 60s rate limit per email                   |
| POST   | `/api/v1/auth/forgot-password`             | None | 60s rate limit per email                   |
| POST   | `/api/v1/auth/reset-password`              | None | Token from email                           |
| POST   | `/api/v1/auth/accept-invite`               | None | Invitation token in body                   |
| GET    | `/api/v1/auth/oidc/{provider_id}/login`    | None | Redirect to IdP                            |
| GET    | `/api/v1/auth/oidc/{provider_id}/callback` | None | IdP callback (validates state cookie)      |

### Authenticated

| Method | Path                           | Auth | Notes                                                       |
| ------ | ------------------------------ | ---- | ----------------------------------------------------------- |
| POST   | `/api/v1/auth/refresh`         | JWT  | Exchange token for new one (idempotent, blocklists old JTI) |
| GET    | `/api/v1/auth/me`              | JWT  | Current user profile                                        |
| POST   | `/api/v1/auth/change-password` | JWT  | Requires current password                                   |
| POST   | `/api/v1/auth/change-email`    | JWT  | Requires password; marks account unverified                 |
| POST   | `/api/v1/auth/change-handle`   | JWT  | Requires password                                           |
| POST   | `/api/v1/auth/logout`          | JWT  | Blocklists token JTI                                        |

### Internal

| Method | Path                                  | Auth          | Notes                                                        |
| ------ | ------------------------------------- | ------------- | ------------------------------------------------------------ |
| GET    | `/api/v1/auth/verify-workspace-token` | Workspace JWT | Used by nginx `auth_request`; returns `X-Token-Error` header |

## Workspaces

### List & Create

| Method | Path                        | Auth | Notes                                                      |
| ------ | --------------------------- | ---- | ---------------------------------------------------------- |
| GET    | `/api/v1/workspaces`        | JWT  | User's own workspaces                                      |
| GET    | `/api/v1/workspaces/shared` | JWT  | Workspaces shared with user                                |
| POST   | `/api/v1/workspaces`        | JWT  | Create workspace; owner gets full ACL; creates role groups |

### Modify

| Method | Path                                | Auth          | Notes                                    |
| ------ | ----------------------------------- | ------------- | ---------------------------------------- |
| PUT    | `/api/v1/workspaces/{id}`           | ACL: edit     | Update name, image, command, mounts, env |
| DELETE | `/api/v1/workspaces/{id}`           | ACL: delete   | Delete workspace and stop container      |
| POST   | `/api/v1/workspaces/{id}/duplicate` | ACL: create   | Clone workspace config                   |
| POST   | `/api/v1/workspaces/{id}/restart`   | ACL: terminal | Stop and remove container                |

### Export & Import

| Method | Path                             | Auth       | Notes                                                  |
| ------ | -------------------------------- | ---------- | ------------------------------------------------------ |
| GET    | `/api/v1/workspaces/{id}/export` | ACL: admin | Stream .tar.gz archive; admin can export any workspace |
| POST   | `/api/v1/workspaces/import`      | JWT        | Create from .tar.gz; sanitizes env vars                |

### Sharing & Members

| Method | Path                                          | Auth       | Notes                                         |
| ------ | --------------------------------------------- | ---------- | --------------------------------------------- |
| GET    | `/api/v1/workspaces/{id}/members`             | ACL: share | List members with access                      |
| POST   | `/api/v1/workspaces/{id}/members`             | ACL: share | Add user (grants view, terminal, files, chat) |
| DELETE | `/api/v1/workspaces/{id}/members/{member_id}` | ACL: share | Remove user                                   |

### Roles

| Method | Path                                               | Auth       | Notes              |
| ------ | -------------------------------------------------- | ---------- | ------------------ |
| GET    | `/api/v1/workspaces/{id}/roles`                    | ACL: share | List role groups   |
| POST   | `/api/v1/workspaces/{id}/roles/{role}`             | ACL: share | Add user to role   |
| DELETE | `/api/v1/workspaces/{id}/roles/{role}/{member_id}` | ACL: share | Remove from role   |
| PATCH  | `/api/v1/workspaces/{id}/roles`                    | ACL: share | Atomic role change |

### Groups & ACL

| Method | Path                                        | Auth       | Notes                    |
| ------ | ------------------------------------------- | ---------- | ------------------------ |
| GET    | `/api/v1/workspaces/{id}/groups`            | ACL: share | List groups with access  |
| POST   | `/api/v1/workspaces/{id}/groups`            | ACL: share | Share with group         |
| DELETE | `/api/v1/workspaces/{id}/groups/{group_id}` | ACL: share | Revoke group access      |
| GET    | `/api/v1/workspaces/{id}/acl`               | ACL: share | Get resolved ACL entries |
| PUT    | `/api/v1/workspaces/{id}/acl`               | ACL: share | Replace all ACL entries  |

## Files

| Method | Path                                     | Auth       | Notes                         |
| ------ | ---------------------------------------- | ---------- | ----------------------------- |
| GET    | `/api/v1/workspaces/{id}/files`          | ACL: files | List directory                |
| GET    | `/api/v1/workspaces/{id}/files/content`  | ACL: files | Read file                     |
| DELETE | `/api/v1/workspaces/{id}/files`          | ACL: files | Delete file or directory      |
| POST   | `/api/v1/workspaces/{id}/files/rename`   | ACL: files | Rename                        |
| GET    | `/api/v1/workspaces/{id}/files/download` | ACL: files | Download as .zip              |
| POST   | `/api/v1/workspaces/{id}/files/upload`   | ACL: files | Upload (default 500 MB limit) |

## Volumes

| Method | Path                     | Auth | Notes                          |
| ------ | ------------------------ | ---- | ------------------------------ |
| GET    | `/api/v1/volumes`        | JWT  | List user's volumes            |
| POST   | `/api/v1/volumes`        | JWT  | Create labeled volume          |
| DELETE | `/api/v1/volumes/{name}` | JWT  | Delete (checks user ownership) |

## Groups

### User APIs

| Method | Path                                    | Auth                | Notes                   |
| ------ | --------------------------------------- | ------------------- | ----------------------- |
| GET    | `/api/v1/groups`                        | JWT                 | List all groups         |
| POST   | `/api/v1/groups`                        | ACL: create         | Create group            |
| PATCH  | `/api/v1/groups/{id}`                   | ACL: edit           | Update name/description |
| DELETE | `/api/v1/groups/{id}`                   | ACL: delete         | Delete group            |
| GET    | `/api/v1/groups/{id}/members`           | ACL: view           | List members            |
| POST   | `/api/v1/groups/{id}/members`           | ACL: manage_members | Add member              |
| DELETE | `/api/v1/groups/{id}/members/{user_id}` | ACL: manage_members | Remove member           |

### Admin APIs

| Method | Path                                          | Auth       | Notes           |
| ------ | --------------------------------------------- | ---------- | --------------- |
| GET    | `/api/v1/admin/groups`                        | ACL: admin | List all groups |
| POST   | `/api/v1/admin/groups`                        | ACL: admin | Create group    |
| PATCH  | `/api/v1/admin/groups/{id}`                   | ACL: admin | Update group    |
| DELETE | `/api/v1/admin/groups/{id}`                   | ACL: admin | Delete group    |
| GET    | `/api/v1/admin/groups/{id}/members`           | ACL: admin | List members    |
| POST   | `/api/v1/admin/groups/{id}/members`           | ACL: admin | Add member      |
| DELETE | `/api/v1/admin/groups/{id}/members/{user_id}` | ACL: admin | Remove member   |

## Admin — Users & Invitations

| Method | Path                                    | Auth       | Notes                                     |
| ------ | --------------------------------------- | ---------- | ----------------------------------------- |
| GET    | `/api/v1/admin/users`                   | ACL: admin | List all users                            |
| POST   | `/api/v1/admin/users`                   | ACL: admin | Create verified user                      |
| DELETE | `/api/v1/admin/users/{id}`              | ACL: admin | Delete user (cannot delete self or agent) |
| PATCH  | `/api/v1/admin/users/{id}`              | ACL: admin | Update email, password, handle            |
| POST   | `/api/v1/admin/invitations`             | ACL: admin | Send invitation email                     |
| GET    | `/api/v1/admin/invitations`             | ACL: admin | List invitations                          |
| DELETE | `/api/v1/admin/invitations/{id}`        | ACL: admin | Revoke invitation                         |
| POST   | `/api/v1/admin/invitations/{id}/resend` | ACL: admin | Resend invitation email                   |

## Admin — ACL

| Method | Path                                        | Auth       | Notes                      |
| ------ | ------------------------------------------- | ---------- | -------------------------- |
| GET    | `/api/v1/admin/acl/tree`                    | ACL: admin | ACL tree summary           |
| GET    | `/api/v1/admin/acl/by-principal/user/{id}`  | ACL: admin | Entries granted to user    |
| GET    | `/api/v1/admin/acl/by-principal/group/{id}` | ACL: admin | Entries granted to group   |
| GET    | `/api/v1/admin/acl/resource`                | ACL: admin | ACL for a resource         |
| PUT    | `/api/v1/admin/acl/resource`                | ACL: admin | Replace ACL for a resource |

## Container→Host Endpoints

These are restricted at the nginx level to container traffic (IP ACL) and validated via workspace JWT (`auth_request`). The backend also validates the workspace JWT as defense-in-depth.

| Method | Path                                   | Auth                   | Notes                            |
| ------ | -------------------------------------- | ---------------------- | -------------------------------- |
| POST   | `/api/v1/browser-delegate`             | Workspace JWT + IP ACL | Relay request to browser tab     |
| POST   | `/api/v1/browser-delegate/stream`      | Workspace JWT + IP ACL | Streaming variant (NDJSON)       |
| POST   | `/api/v1/workspaces/post-chat-message` | Workspace JWT + IP ACL | Post chat message from container |

The LLM proxy (`/llm-proxy/...`) is also gated by workspace JWT + IP ACL but is handled entirely by nginx (proxies to `$KLANGK_LLM_BASE_URL` with injected API key).

## Utility

| Method | Path                     | Auth | Notes                           |
| ------ | ------------------------ | ---- | ------------------------------- |
| GET    | `/api/v1/users/search`   | JWT  | Search users by email/handle    |
| GET    | `/api/v1/images`         | JWT  | List available container images |
| GET    | `/api/v1/my-permissions` | JWT  | User's effective permissions    |

## WebSocket

| Path  | Auth                          | Notes                                               |
| ----- | ----------------------------- | --------------------------------------------------- |
| `/ws` | JWT via `?token=` query param | Close codes: 4001 (missing/invalid), 4002 (expired) |

## Test-Only Endpoints

Available only when `KLANGK_TEST_MODE` is set.

| Method | Path                                | Auth | Notes                      |
| ------ | ----------------------------------- | ---- | -------------------------- |
| GET    | `/api/v1/test/idle-timeout`         | None | Get idle timeout           |
| POST   | `/api/v1/test/set-idle-timeout`     | None | Set idle timeout           |
| GET    | `/api/v1/test/workspace-token/{id}` | None | Generate workspace JWT     |
| GET    | `/api/v1/test/browsers/{id}`        | None | List browser registrations |

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
