# Handles

Every Klangk user has a unique handle (e.g., `@alice`). Handles are
used throughout the platform:

- **Terminal** — under the per-handle home layout, your `$HOME`
  directory is `/home/<handle>/` (see
  [Workspaces](workspaces.md#home-directory-layout))
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

How your handle relates to your home directory depends on the
workspace's [home layout](workspaces.md#home-directory-layout), chosen
when the workspace is created:

- **Shared home** (the default) — every member's `$HOME` is the single
  `/home/klangk`. Your handle does not affect your home directory path.
- **Per-handle home** — your `$HOME` is `/home/<handle>/`, a symlink to
  `.users/<user-id>/` on the host filesystem.

On a per-handle workspace, changing your handle changes your `$HOME`
path on your next terminal session — but the underlying directory
(keyed by user ID) stays the same. Your files, dotfiles, and history
are preserved.

## Changing your handle

You can change your handle from the Settings page. The new handle
must be unique and follow the naming rules above. A password
confirmation is required.

Admins can change any user's handle from the Admin panel without
needing the user's password.
