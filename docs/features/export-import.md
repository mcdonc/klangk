# Workspace Export & Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, instance ID, image, service command, mounts, env vars, num_ports, home layout)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)
- `CLASSIFICATION.txt` — classification banner, only present when `KLANGKD_EXPORT_CLASSIFICATION` is set (see below)

## Export

Export requires the **`export`** permission on the `/admin` resource (`GET /api/v1/workspaces/{id}/export`). The seeded admin-group wildcard grant (`Allow group:admin *` on `/admin`) covers it, so existing admins can export out of the box. `klangk export <workspace>` downloads the archive; the tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

### Disabling export

To revoke bulk export on an instance that never wants it, add a **Deny** ACE for the `export` permission on the `/admin` resource, targeting the **Everyone** system principal, positioned **ahead of** the wildcard allow (lower position = checked first):

| Resource | Action | Principal   | Permission |
| -------- | ------ | ----------- | ---------- |
| `/admin` | Deny   | Everyone    | `export`   |
| `/admin` | Allow  | group:admin | `*`        |

This blocks export for everyone — including admins — while leaving every other admin capability (user management, invitations, groups, ACL editing) untouched. Import is unaffected: it checks the `create` permission on `/workspaces`.

Add the entry via **Admin → ACL** in the web UI (select the `/admin` resource) or `PUT /api/v1/admin/acl/resource`. To grant export back to a narrow set of users, add an **Allow** `export` ACE for that user/group at an even lower position.

### Classification marking

Set `KLANGKD_EXPORT_CLASSIFICATION` (e.g. `CONFIDENTIAL // INTERNAL ONLY`) to stamp every exported archive with the text:

- a `CLASSIFICATION.txt` file at the archive root, and
- an `X-Classification` response header on the export response.

Empty (the default) exports unmarked archives. The banner also applies to the per-workspace archives Klangk builds when a user account is deleted. Re-importing a marked archive is unaffected — import reads only `workspace.json` and `home/`.

## Import

`klangk import <archive>` uploads the archive via `POST /api/v1/workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

The workspace's **home layout** (per-handle vs shared, see
[Workspaces](workspaces.md)) is preserved: `workspace.json` carries the
exported layout, and the import honors it even when the server's default
(`KLANGKD_PER_HANDLE_HOME`) differs. Archives exported before the layout
feature imported as per-handle (the only layout at the time).

> **Same-instance only:** Archives include the exporting instance's unique ID. Import rejects archives that are missing an instance ID or whose instance ID does not match the importing server. This prevents foreign workspace imports from planting home directory symlinks that reference user IDs that don't exist on the destination instance.

System-level packages (apt installs, etc.) are not included — those belong in custom workspace images.
