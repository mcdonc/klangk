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

## Terminal Tabs

Each user has their own set of terminal tabs. Tabs map 1:1 to tmux windows
inside the container. All tabs share a single tmux session named by the
user's ID, so switching tabs is instant — no new process is started.

![Initial terminal with a single tab](../assets/terminal-sharing/01-initial-terminal.png)

- Click **+** to create a new terminal tab (tmux window)
- Click a tab to switch to it
- Click **✕** on a tab to close it (only shown when more than one tab exists)
- Right-click a tab to open a context menu with **Rename** and **Share/Unshare**

### Renaming Tabs

Right-click any tab and select **Rename** to change its display name. The
name is stored as the tmux window name, so it persists across reconnections
and is visible to other users if the tab is shared.

![Two terminal tabs — one shared, one isolated](../assets/terminal-sharing/06-two-tabs.png)

## Shared Terminals

Any terminal tab can be promoted to a shared terminal, making it visible
and joinable by other workspace members. Sharing is per-tab — you can share
one tab while keeping others private.

### Sharing a Tab

Right-click a tab and select **Share**. The tab gains a broadcast icon
(📡) indicating it is now shared. Other workspace members see the shared
tab appear in their tab bar.

![Tab with broadcast icon indicating it is shared](../assets/terminal-sharing/04-shared-tab-with-icon.png)

To unshare, either:

- Right-click the tab and select **Unshare**, or
- Click the broadcast icon directly

### Joining a Shared Terminal

Shared terminals from other users appear in your tab bar with a prefix
showing the owner's handle (e.g. `alice:build`). Click the tab to join —
you are now seeing the same terminal session as the owner.

![Collaborator's view showing a shared terminal from another user](../assets/terminal-sharing/08-collaborator-view.png)

Depending on your role, you may be able to type (read-write) or only
watch (read-only). A 🔒 icon indicates read-only access.

### Viewer Tracking

When someone joins your shared terminal, a 👁 icon with a count appears
on the tab showing how many users are currently viewing.

![Shared tab showing one viewer](../assets/terminal-sharing/07-viewer-count.png)

Hover over the tab to see a tooltip listing the full tab name and the
email handles of all current viewers.

### How It Works

Under the hood, each user's terminals run in a single tmux session inside
the workspace container. When a user joins a shared terminal, they create
a new tmux session in the same **session group** as the owner's session.
Session groups share the same set of windows, so all participants see the
same content in real time.

Each joiner gets an independent tmux session (separate scroll position,
active window) but shares the underlying window panes. When a joiner
disconnects, their session is cleaned up automatically.

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

- **Owners** can share/unshare tabs, type in shared terminals, and rename tabs.
- **Coders** can watch shared terminals (read-only) but cannot share their own
  tabs or type in others' shared terminals. They have full isolated terminal
  and file access.
- **Collaborators** can type in shared terminals but cannot share their own tabs.
  They have full isolated terminal and file access.
- **Spectators** can watch shared terminals in read-only mode. They cannot start
  isolated terminals or access files.
