# Workspace Export & Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, image, default command, mounts, env vars, num_ports)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)

## Export

Export is admin-only. `klangk export <workspace>` downloads the archive via `GET /workspaces/{id}/export`. The tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

## Import

`klangk import <archive>` uploads the archive via `POST /workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

System-level packages (apt installs, etc.) are not included — those belong in custom workspace images.
