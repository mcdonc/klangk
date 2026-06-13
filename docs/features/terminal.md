# Terminal

- Direct shell access to the workspace container via the Terminal tab in the right panel
- Uses flterm (Flutter terminal emulator backed by libghostty) with xterm.dart as a fallback, dark theme (Tomorrow Night palette)
- Backend spawns `podman exec` subprocess with PTY (`os.openpty`) piped over the existing WebSocket
- Runs as `klangk` user in `/home/klangk/work` with bash, tab completion, readline, colored prompt/ls, and persistent history (defaults from `/etc/bash.bashrc` in the image, overridable via `~/.bashrc` on the persistent home mount). History is flushed to `~/.bash_history` after each command via `PROMPT_COMMAND` so it survives terminal kills.
- Terminal interaction bumps the container idle timeout via `record_activity()`
- On-demand: subprocess starts when user clicks the Terminal tab
- State preserved across tab switches (IndexedStack keeps all panels alive)
- Right-click context menu with Copy (when text selected) and Paste
- Scrollbar for terminal history
- Overlay with restart button when container stops (idle timeout or unexpected), auto-reconnects terminal session after restart
- Cleaned up on workspace disconnect or WebSocket close

## Shared Terminals

Shared terminals are visible to all workspace members with appropriate permissions.
Each shared terminal runs as an independent tmux server with a named socket at
`/home/.terminals/<name>.sock` inside the container. Users join via tmux session
groups, so each user has an independent view (scroll position, etc.) of the same
terminal.

### Role Permissions

| Permission                     | Owners | Coders | Collaborators | Spectators |
| ------------------------------ | ------ | ------ | ------------- | ---------- |
| `terminal`                     | ✓\*    | ✓      | ✓             | ✓          |
| `code-in-isolation`            | ✓\*    | ✓      | ✓             |            |
| `share-terminals`              | ✓\*    |        |               |            |
| `code-in-shared-terminals`     | ✓\*    |        | ✓             |            |
| `spectate-on-shared-terminals` | ✓\*    | ✓      | ✓             | ✓          |
| `files`                        | ✓\*    | ✓      | ✓             |            |
| `chat`                         | ✓\*    | ✓      | ✓             | ✓          |

\* Owners have the wildcard (`*`) permission which implies all permissions.

- **Owners** can create, delete, and type in shared terminals.
- **Coders** can watch shared terminals (spy mode) but not create or type in them.
  They have full isolated terminal and file access.
- **Collaborators** can type in shared terminals but not create or delete them.
  They have full isolated terminal and file access.
- **Spectators** can watch shared terminals in read-only mode. They cannot start
  isolated terminals or access files.
