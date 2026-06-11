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
