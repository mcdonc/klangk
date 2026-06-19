# Features

Klangk provides a rich set of features for multi-user AI collaboration:

- [**Authentication**](authentication.md) — Email/password, OIDC/SSO, brute-force protection
- [**Authorization**](authorization.md) — Pyramid-style ACL system with resource tree and principals
- [**Admin Management**](admin-management.md) — User/group management, ACL editing, user archival
- [**Invitations**](invitations.md) — Admin invitation workflow for onboarding new users
- [**Workspaces**](workspaces.md) — Isolated coding environments with sharing, port allocation, export/import
- [**Terminal**](terminal.md) — Full terminal emulator with Pi agent integration, idle timeout, session persistence
- [**Chat**](chat.md) — Real-time workspace chat with markdown, @mentions, message types, container-to-chat API
- [**File Viewer**](file-viewer.md) — Directory tree, drag-and-drop upload, preview, download
- [**AI Coding Harnesses**](ai-coding-harnesses.md) — Pi and Claude Code agents pre-installed in every workspace
- [**Pi Extensions**](plugins.md) — Server-side and client-side extensions via TypeScript and Dart
- [**Container Packages**](container-packages.md) — Pre-installed languages, tools, and CLI utilities in workspace containers
- [**CLI**](../reference/cli.md) — Command-line client for managing workspaces, shells, and file sync
- [**SSH Agent Forwarding**](ssh-agent-forwarding.md) — Forward local SSH keys into containers via `klangkc shell`
- [**GitHub HTTPS Authentication**](github-authentication.md) — HTTPS git credentials via browser-based PAT dialog

## UI/Theme

- Dark theme inspired by GitHub's dark default (dark canvas background, surface cards, muted borders)
- Centralized color palette in `KColors` (`src/frontend/lib/theme/colors.dart`) — named tokens for backgrounds, borders, text, and accents
- Green Klangk logo (robot icon + lowercase "klangk" text) with matching green FAB and primary action buttons
- Full-width Terminal+Files tabs with background-only active state (no indicator bar), rounded bottom corners
- Slidable Debug panel on bottom (collapsed by default)
- Browser tab title updates per page ("Klangk - Login", "Klangk - Workspaces", "Klangk - workspace-name")

## Panel Layout

- Two-part split: tabbed panel on top (Terminal, Files tabs) and slidable Debug panel on bottom
- Debug panel collapsed by default, expandable via draggable horizontal divider
- All panels stay alive across switches (IndexedStack for tabs, always-mounted Debug)
- Debug pane receives events from the start, even before first viewed

## Debug Panel

- Container lifecycle events (starting, ready with port info and status, idle stop, restart)
- Session resume notifications
- Query text shown for each prompt sent
- Tool call entries from Pi (including extension tools)
- Error entries
- Timestamps and color-coded entries
- Selectable text for titles and content
- Clear button
