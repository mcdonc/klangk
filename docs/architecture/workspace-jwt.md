# Workspace JWT Auth

Each container receives a workspace JWT at startup, written to `/tmp/klangk/workspace-token` by the backend via `klangk-set-workspace-token`. Container processes read the token dynamically via `klangk-workspace-token` and include it as `Authorization: Bearer <token>` in HTTP requests to the host. The proxy validates it via `auth_request` before allowing access to:

- `/llm-proxy` — LLM API proxy (injects the real API key upstream)
- `/api/v1/browser-delegate` — browser-delegate bridge for Pi extensions
- `/api/v1/workspaces/post-chat-message` — containers can post chat messages

The token uses the same `KLANGKD_JWT_SECRET` as user JWTs but is distinguished by a `purpose: "workspace"` claim. Token lifetime is controlled by `KLANGKD_WORKSPACE_TOKEN_HOURS` (default 24h). Tokens are automatically renewed at 80% of their lifetime — the backend generates a new token and writes it to the container via `podman exec`. Pi resolves `!klangk-workspace-token` fresh on every LLM request (no cache), so all processes — including the long-lived chat agent — pick up renewed tokens automatically. The proxy's IP-based ACLs restrict these endpoints to container traffic as defense-in-depth alongside JWT validation.
