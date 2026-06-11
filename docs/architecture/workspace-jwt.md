# Workspace JWT Auth

Each container receives a `KLANGK_WORKSPACE_TOKEN` environment variable at startup — a signed JWT identifying the workspace. Containers include this token as `Authorization: Bearer <token>` in HTTP requests to the host. Nginx validates it via `auth_request` before allowing access to:

- `/llm-proxy` — LLM API proxy (injects the real API key upstream)
- `/api/browser-delegate` — browser-delegate bridge for Pi extensions
- `/api/workspace/post-chat-message` — containers can post chat messages

The token uses the same `KLANGK_JWT_SECRET` as user JWTs but is distinguished by a `purpose: "workspace"` claim. Token lifetime is controlled by `KLANGK_WORKSPACE_TOKEN_HOURS` (default 24h). IP-based ACLs (`CONTAINER_ACL`) remain as defense-in-depth alongside JWT validation.
