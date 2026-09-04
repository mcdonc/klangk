# Workspace Export & Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, instance ID, image, service command, mounts, env vars, num_ports, home layout)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)

## Export

Export requires the **`export-workspace`** permission on the workspace's own resource (`GET /api/v1/workspaces/{id}/export` checks `/workspaces/{id}`). Both seeded grants cover it out of the box:

- the owner's wildcard ACE (`Allow user:{id} *`), and
- the `owners-<workspace_id>` role group's wildcard grant.

So owners (and anyone added to the workspace's owners role) can export their own workspaces. Admins no longer blanket-export workspaces they hold no grant on — an admin must be an owner, an owners-role member, or hold an explicit `export-workspace` ACE. `klangk export <workspace>` downloads the archive; the tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

### Disabling export per workspace

To revoke export on a workspace, add a **Deny** ACE for the `export-workspace` permission on that workspace's resource, targeting the **Everyone** system principal, positioned **ahead of** the wildcard allows (lower position = checked first):

| Resource           | Action | Principal           | Permission         |
| ------------------ | ------ | ------------------- | ------------------ |
| `/workspaces/{id}` | Deny   | Everyone            | `export-workspace` |
| `/workspaces/{id}` | Allow  | user / owners group | `*`                |

The ACL walk is first-match-wins from the workspace resource upward, so the deny must sit on the workspace itself (a deny higher in the tree never gets consulted once the workspace-level wildcard matches). This blocks export for everyone on that workspace while leaving the owner's other capabilities untouched. Import is unaffected: it checks the `create-workspace` permission on `/workspaces`.

> **Not a hard guarantee:** the workspace ACL is writable by anyone holding the `share-advanced` permission — which the owner wildcard grants. An owner (or any share-advanced holder) can edit the workspace ACL and delete the deny entry, re-enabling export. The deny is effective against principals who _cannot_ rewrite the workspace ACL (members, spectators, coders, and share-only collaborators); to revoke export against the owner too, remove them from the owners role / wildcard grant or take the workspace away — or block the endpoint at the reverse proxy.

Add the entry via the workspace **Sharing → Advanced ACL** editor in the web UI or `PUT /api/v1/acl/resource`. To grant export to a narrow set of users on a workspace, add an **Allow** `export-workspace` ACE for that user/group at an even lower position. In the web UI the Export card appears in the workspace **Settings** tab (which needs `edit-workspace`); a user granted only `export-workspace` should use the CLI (`klangk export`) or the API directly.

## Import

`klangk import <archive>` uploads the archive via `POST /api/v1/workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

The workspace's **home layout** (per-handle vs shared, see
[Workspaces](workspaces.md)) is preserved: `workspace.json` carries the
exported layout, and the import honors it even when the server's default
differs. Archives exported before the layout
feature imported as per-handle (the only layout at the time). Note the
importing deploy's ceiling still applies: an archive carrying
`per_handle_home: true` imports onto a `KLANGKD_PER_HANDLE_HOME=false`
server as a stored-but-inert value — the workspace gets the shared
home (clamped at every connect/start, like any other stored `true`).

> **Same-instance only:** Archives include the exporting instance's unique ID. Import rejects archives that are missing an instance ID or whose instance ID does not match the importing server. This prevents foreign workspace imports from planting home directory symlinks that reference user IDs that don't exist on the destination instance.

System-level packages (apt installs, etc.) are not included — those belong in custom workspace images.
