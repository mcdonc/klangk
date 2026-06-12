# Human-Human-AI Collaboration Features for Klangk

## Context

This plan replaces the original herdr-centric collaboration spec. After researching both herdr and tmux internals, we determined that **tmux is the right foundation for terminal sharing and isolation**, while herdr remains a user-facing application that users can run once they have a terminal session.

**Why tmux over herdr for the sharing layer:**

- tmux has built-in multi-user ACLs (`server-access` command) with per-user/group read-only enforcement
- Read-only is enforced at multiple layers: socket permissions, ACL join, command dispatch, and key input handling
- herdr has zero multi-user features — no auth, no ACLs, no read-only mode
- Adding multi-user to herdr would mean forking a fast-moving AGPL project to add features orthogonal to its purpose

**Key limitation:** tmux ACLs are server-wide, not per-session. This means each terminal needs its own tmux server (separate socket) for isolation.

## Entity Model

- **Workspace**: Long-lived collaboration space with container, filesystem, chat history, and sharing rules
- **Terminal**: A tmux server instance within the container, with its own socket and independent ACLs. Persistent across disconnects.
- **Connection**: Ephemeral WebSocket link from a user to a workspace. User first connects to workspace (gets chat, presence, file access), then optionally attaches to a terminal.
- **User**: Klangk-authenticated identity that can maintain multiple concurrent connections

```text
User ──── Connection ──── Workspace ──── Terminal (shared)
 │              │              │              │
 │              │              │         tmux server
 │              │              │         + session group
 │              │              │
 │              │              ├──── Terminal (isolated)
 │              │              │         tmux server
 │              │              │         in user's $HOME
 │              │              │
 │              │              ├──── Chat
 │              │              └──── Files (/home/klangk/)
 │              │
 │         WebSocket (ephemeral)
 │
 └──── Home dir (/home/users/<uuid>/)
            ├── .terminals/    (isolated sockets)
            ├── .gitconfig
            ├── .ssh/
            └── .bash_history
```

A Connection is to a Workspace, not to a Terminal. The user first connects to the workspace (gets chat, presence, file access), then optionally attaches to a terminal.

## Unix User Model

Single `klangk` unix user for everything. No second user needed.

- All files owned by `klangk` — no mixed-ownership problems
- Spy mode is enforced by tmux's `-r` flag on attach, not by OS-level user isolation
- Safety relies on the "no shell underneath" constraint: the initial command is `tmux attach`, and when the user exits/detaches, the connection ends — there's no opportunity to re-attach without `-r`

## Terminal Architecture

Each terminal is an independent tmux server using **session groups** for multi-user isolation:

```text
Terminal "dev" → tmux -S /home/klangk/.terminals/dev.sock new-session -s dev
Terminal "test" → tmux -S /home/klangk/.terminals/test.sock new-session -s test
```

**Session groups:** Each user gets their own session within a shared group, rather than everyone attaching to the same session. This provides:

- **Independent navigation**: User A can be on window 1 while user B is on window 3
- **Per-user environment**: Each session has its own `KLANGK_USER`, `KLANGK_USER_EMAIL`, etc. via `set-environment` — critical for git attribution, audit logging, and the eventual credential helper
- **Shared window list**: When any user creates a window, it appears in all grouped sessions automatically

```bash
# First user (creates the group):
tmux -S <socket> new-session -s alice -e KLANGK_USER=alice@example.com

# Subsequent users (join the group):
tmux -S <socket> new-session -t alice -s bob -e KLANGK_USER=bob@example.com

# Spy user (joins group read-only):
tmux -S <socket> new-session -t alice -s charlie -e KLANGK_USER=charlie@example.com \; \
  attach-session -r
```

**Terminal types:**

| Type     | Implementation              | Socket location                       | Other users can join?                    | Session persistence? |
| -------- | --------------------------- | ------------------------------------- | ---------------------------------------- | -------------------- |
| Shared   | tmux server + session group | `/home/klangk/.terminals/<name>.sock` | Yes (collaborate/spy)                    | Yes                  |
| Isolated | tmux server (no group)      | `$HOME/.terminals/<workspace>.sock`   | No (socket not discoverable via backend) | Yes                  |

**Shared terminal access modes:**

| Mode        | Attach method                     | Can type?          | Own environment? | Independent window navigation? |
| ----------- | --------------------------------- | ------------------ | ---------------- | ------------------------------ |
| Collaborate | New session in group + new window | Yes                | Yes              | Yes                            |
| Spy         | New session in group + `-r`       | No (tmux-enforced) | Yes              | Yes                            |

- Each collaborator gets their own session in the group with their own `KLANGK_USER` environment, and a new window for their shell
- Spy users join the group read-only — can navigate windows but can't type
- When the user exits/detaches, their session in the group is destroyed — no shell underneath
- herdr is just an application users can launch inside their tmux session (e.g. for agent orchestration UI)

**Isolated terminals** are tmux servers with sockets stored in the user's private home directory rather than the shared terminals directory. The backend doesn't expose their socket paths to other users, so they're not discoverable through normal means. Still the same `klangk` unix user, so a determined user could find the socket by searching `/home/users/`, but it's a very different bar than `tmux list-sessions` on a shared server.

**Per-user home directories:** Each user gets `HOME=/home/users/<uuid>`. This provides:

```text
/home/klangk/                        # shared workspace filesystem
/home/klangk/.terminals/              # shared terminal sockets
/home/users/<uuid>/                  # per-user home
/home/users/<uuid>/.terminals/        # isolated terminal sockets
/home/users/<uuid>/.bash_history     # per-user shell history
/home/users/<uuid>/.gitconfig        # per-user git identity
/home/users/<uuid>/.ssh/             # per-user SSH keys
```

The backend sets `HOME=/home/users/<uuid>` when launching any session for a user. This naturally solves git identity (per-user `~/.gitconfig`), shell history separation, and isolated socket placement — all without multiple unix users.

**Backend integration:**

- Socket owned by `klangk` (the only user)
- Klangk backend is the ACL authority — decides who can connect to which socket and in what mode
- Backend launches the appropriate `tmux new-session` command with user-specific environment and group membership
- Backend attaches a **control-mode client** (`tmux -S <socket> attach -C`) to each tmux server for monitoring — receives structured events (client-attached, client-detached, window-created, etc.) without parsing terminal output
- `KLANGK_USER` is injected via the shell command for each user's window (not via tmux `set-environment`, which doesn't propagate to grouped sessions)

## Features (Priority Order)

### Phase 1: Terminal Model

- No terminals table — running state is discovered from tmux sockets on the filesystem
- Terminal CRUD via backend API (create socket, list by scanning socket dir, delete by killing server)
- Access control is at the **workspace level**, not per-terminal: if you're invited to a workspace, you can see all its terminals
- Workspace ACL role determines terminal mode: collaborate (read-write) or spy (read-only) across all terminals
- Backend spawns tmux server per terminal with dedicated socket path
- Per-user home directories created on first connection (`/home/users/<uuid>/`)
- Frontend terminal list UI showing available terminals per workspace

### Phase 1.5: Terminal Pane UI (aligns with #201)

The terminal pane gets two top-level tabs, each with subtabs per tmux window:

```text
Terminal Pane
├── Mine                          (isolated tmux session)
│   ├── bash (window 0)
│   ├── build (window 1)
│   └── logs (window 2)
└── Shared                        (shared tmux session group)
    ├── alice (window 0)
    ├── bob (window 1)
    └── deploy (window 2)
```

- **Mine**: connects to `$HOME/.terminals/<workspace>.sock` — user's isolated session
- **Shared**: connects to `/home/klangk/.terminals/default.sock` — the shared session group
- Each subtab is a tmux window; panes within a window render as splits in the terminal view
- Spy-role users do not see the Mine tab
- New windows created via UI or from within the terminal (tmux/herdr)
- Backend discovers windows via control-mode client

### Phase 2: Terminal Attach + Sharing

- WebSocket-to-PTY bridge: backend attaches to tmux socket and relays I/O to browser via xterm.js
- Each user gets their own session in the terminal's session group, with `KLANGK_USER` injected via `set-environment`
- Collaborate mode: forward keystrokes to tmux
- Spy mode: session attached with `-r` (read-only, tmux-enforced)
- Multiple users attach simultaneously with independent window navigation but shared window list
- Backend attaches a control-mode client (`-C`) per terminal for structured event monitoring
- Terminal shows who's attached and in what mode (via control-mode events or `#{window_active_clients_list}`)

### Phase 3: Presence + Awareness

- ~~Already partially shipped (#181)~~ — workspace-level presence exists
- Extend to show which terminal each user is attached to and their mode (collaborate/spy)
- Presence bar shows per-terminal user indicators

### Phase 4: Chat Enhancements

- ~~@mentions shipped (#180)~~
- Message types: user / agent / system
- Container→chat REST API: agent processes can post messages to workspace chat
- Markdown rendering for code sharing

### Phase 5: CLI Integration

- `klangk shell <workspace>` — isolated terminal (default), own tmux in `$HOME/.terminals/`
- `klangk shell <workspace> --shared` — create/join the default shared terminal
- `klangk shell <workspace> --shared --name <name>` — create/join a named shared terminal
- `klangk shell <workspace> --spy` — spy on the default shared terminal
- `klangk shell <workspace> --spy --name <name>` — spy on a named shared terminal
- AI agents connect via CLI identically to humans
- CLI users are equal participants with browser users

### Phase 6: Git Identity & Signing

- Per-session environment variables (`KLANGK_USER`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`) already injected via session groups (Phase 2) — git attribution works automatically
- Per-user SSH signing keys for commit integrity
- Signatures verified by GitHub natively (no custom app needed)

### Phase 7: Browser-Delegated Git Credentials

- Custom `git-credential-klangk` helper in the container
- Routes auth requests through WebSocket bridge to user's browser
- Browser provides cached OAuth tokens
- Ephemeral tokens — never stored in container

### Phase 8: Terminal Audit & Transcripts

- `terminal_transcripts` table: stores terminal output, keyed by terminal name + workspace_id + timestamp range
- Capture via control-mode client (already attached in Phase 2) or `pipe-pane -o` streaming to a host-side unix socket
- Host-side listener receives output, tags with terminal/user identity (available from session-level `KLANGK_USER`), stores in DB
- Transcripts persist after the tmux server is gone — enables review of terminals that no longer exist
- Terminal list UI can show both live terminals (from socket scan) and historical terminals (from DB)

### Phase 9: File Compartmentalization

- Mount namespace isolation per terminal session
- Workspace admins define "file zones" with access rules
- Terminals see only permitted directories
- Single `klangk` unix user throughout — no mixed ownership

## Open Question: Keybindings and User-Facing Terminal UX

**Problem:** tmux keybindings conflict with browsers (Ctrl+W, Ctrl+T, etc.), with apps inside the terminal (emacs, vim), and with user expectations for "normal" terminal behavior (mouse scroll for scrollback, Shift+PgUp/PgDn without a prefix key). This affects both web and CLI users.

**Proposed approach: tmux is invisible infrastructure.** Strip tmux of all user-facing keybindings (no prefix key). Users never interact with tmux directly. Instead:

- **Web users**: the browser UI handles window/pane management, scrollback, etc. Backend drives tmux via control mode. xterm.js handles mouse scroll locally.
- **CLI users**: herdr (or a similar user-facing multiplexer) runs inside the tmux session, providing the keybinding/pane/tab UX. herdr captures all keys; tmux is a pass-through layer for session persistence, sharing, and read-only enforcement.

This means the architecture is:

```text
Web user  → browser → xterm.js → WebSocket → backend → tmux (control mode)
CLI user  → klangk shell → herdr (inside container) → tmux session → shell
```

Both see the same tmux windows/panes through different frontends. Neither types tmux commands. herdr handles keybindings for CLI; web UI handles them for browser.

**Nesting concern:** CLI path is herdr → tmux → shell. If tmux has no keybindings and no prefix key, it's truly invisible — herdr captures all keys, tmux is just a pass-through. Needs prototyping to confirm escape sequences and mouse handling work cleanly through the nesting.

**Not yet resolved — needs prototyping and further discussion.**

## Open Question: Export/Import with Per-User Home Directories

The per-user home directories (`/home/users/<uuid>/`) contain shell history, dotfiles, git config, SSH keys, and isolated terminal sockets. This impacts workspace export/import:

- What gets exported? Shared workspace files (`/home/klangk/`) are straightforward, but per-user homes contain a mix of useful context (dotfiles, history) and sensitive data (`.ssh/`, credentials).
- Socket files (`.terminals/`) are runtime state and should never be exported.
- On import, user UUIDs may differ between instances, so home directory paths won't match the original users.
- Needs a clear policy: always export user homes, never export them, or export selectively (skip `.ssh/`, `.terminals/`).

## Design Decisions

1. **Single `klangk` unix user.** Mixed-ownership filesystems are an operational nightmare. Spy mode is enforced by tmux's `-r` flag, not by OS-level user isolation. The "no shell underneath" constraint makes a second user unnecessary.

2. **One tmux server per terminal, not per workspace.** Each terminal gets its own socket for independent lifecycle management. Note: since all terminals run as the same user and access is workspace-level, a collaborate user could theoretically reach any socket in the workspace. Per-terminal isolation, if ever needed, would require mount namespace isolation (Phase 8).

3. **herdr is a user application, not infrastructure.** Users can run herdr inside their tmux session for agent orchestration, but the sharing/ACL/isolation layer is tmux.

4. **Application-layer ACL is primary.** Klangk's backend is the source of truth for who can access which terminal and in what mode. It controls the tmux attach command flags.

5. **No shell underneath tmux.** The terminal's initial command is `tmux new-session` (into a group). When the user exits, the connection ends. This prevents spy users from escaping to an interactive shell.

6. **Session groups over shared sessions.** Each user gets their own tmux session within a group, rather than everyone attaching to the same session. This enables per-user environment variables (critical for git identity, audit, and credential delegation) and independent window navigation, while keeping a shared window list.

7. **Control-mode backend observer.** The backend attaches a control-mode client (`-C`) to each tmux server for structured event monitoring. This provides presence, activity, and audit data without parsing terminal output or needing separate IPC.
