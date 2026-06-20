# Handles

Every Klangk user has a unique handle (e.g., `@alice`). Handles are
used throughout the platform:

- **Chat** — @mention other users in workspace chat messages
- **Terminal** — your `$HOME` directory is `/home/<handle>/`
- **Presence** — avatar tooltips show your handle in the workspace
- **Shared terminals** — shared tabs are prefixed with the owner's
  handle (e.g., `alice:build`)

## How handles are assigned

When you first create an account, your handle is derived from the local
part of your email address (e.g., `alice@example.com` becomes `alice`).
If that handle is already taken, a numeric suffix is appended
(`alice-2`, `alice-3`, etc.).

Handles must be lowercase and may contain letters, digits, dots,
dashes, and underscores.

## Handle and HOME directory

Your handle determines your home directory path inside workspace
containers. When you open a terminal, `$HOME` is set to
`/home/<handle>/`, which is a symlink to `.users/<user-id>/` on the
host filesystem.

If you change your handle, your `$HOME` path changes on your next
terminal session — but the underlying directory (keyed by user ID)
stays the same. Your files, dotfiles, and history are preserved.

## Changing your handle

You can change your handle from the Settings page. The new handle
must be unique and follow the naming rules above. A password
confirmation is required.

Admins can change any user's handle from the Admin panel without
needing the user's password.
