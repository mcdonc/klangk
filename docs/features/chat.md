# Chat

- Per-workspace chat panel with real-time message broadcasting via WebSocket
- Messages rendered as Markdown (syntax-highlighted code blocks, links, inline formatting)
- Three message types: user (MSG_USER=0), agent (MSG_AGENT=1), system (MSG_SYSTEM=2) with distinct visual styling (system = centered muted italic, agent = robot icon cyan)
- @mention support: `@email` resolves to workspace members, stored in `chat_mentions` table, notified on connect
- Message deletion by author (soft-delete, replaces text with placeholder)
- Chat history pagination: cursor-based (scroll-to-top loads older messages via `chat_load_more` WebSocket command)
- Message history recall: up/down arrows cycle through previously sent messages
- Input: Tab autocomplete for @mentions, Shift+Enter for newline, emacs keybindings (Ctrl+A/E/K), long message collapsing
- **Container-to-chat API**: `POST /api/workspace/post-chat-message` allows containers to post MSG_AGENT messages using the workspace JWT. Validated by nginx `auth_request` + IP ACL.
- System messages broadcast on user join/leave
