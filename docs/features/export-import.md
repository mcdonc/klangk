# Workspace Export & Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, image, service command, mounts, env vars, num_ports)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)

## Export

Export is admin-only. `klangkc export <workspace>` downloads the archive via `GET /api/v1/workspaces/{id}/export`. The tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

## Import

`klangkc import <archive>` uploads the archive via `POST /api/v1/workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

> **Note:** Importing archives exported from a different Klangk instance is not yet supported. Home directory symlinks in the archive reference the exporting user's internal ID, which causes conflicts when imported by a different user. For now, only import archives that were exported from the same instance.

System-level packages (apt installs, etc.) are not included — those belong in custom workspace images.
