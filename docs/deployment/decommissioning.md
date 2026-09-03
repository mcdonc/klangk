# Decommissioning an Instance

**Decommissioning** is retiring a klangk instance permanently — not a
restart, not an upgrade. The goals: users are warned before their
workspaces disappear, data is exported or destroyed according to a
deliberate decision (not by accident), and nothing is left running or
pointing at the dead instance afterwards.

This chapter covers the notification chain (who must be told, and in
what order) and the shutdown and disposal steps for each deployment
mode.

## The notification chain

klangk has no built-in broadcast mechanism — invitation emails are
one-off messages to new users, not a channel for reaching everyone.
Notify out of band, using whatever channel normally reaches your users
(mail list, chat, issue tracker). Announce the shutdown date as far
ahead as your users need; two weeks is a reasonable minimum for an
instance with active daily users.

Notify, in this order:

| Who                       | Why                                                                | What to tell them                                                                                                                                                                        |
| ------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **End users**             | Their workspaces, home directories, and terminal sessions go away. | The shutdown date, and how to get their files out before it (see below).                                                                                                                 |
| **Other admins**          | They share responsibility for the data.                            | The shutdown date, and the decision on archiving vs. destroying `data_dir`.                                                                                                              |
| **Integrators**           | Their tooling breaks silently when the instance goes away.         | Anything scripted against the instance must be repointed: `klangk` CLI server entries (`klangk.yaml`), API tokens, hosted app URLs (`/hosted/{workspace}/{port}/`).                      |
| **Infrastructure owners** | DNS and firewall entries outlive the instance.                     | The DNS record, reverse-proxy route (see [Behind a Reverse Proxy](behind-a-proxy.md)), Tailscale ACL entries, and any published ports (e.g. `8997`) can be removed on the shutdown date. |

## Before shutdown: get the data out

- **Export workspaces that must survive.** Export requires the
  `export-workspace` permission on the workspace — owners have it out of the box,
  and admins must be granted it (or be added to a workspace's
  owners role) to export: `klangk export <workspace>` writes a `.tar.gz`
  of the workspace's home directory and metadata (see
  [Workspace Export & Import](../features/export-import.md)). Note the
  **same-instance-only** rule: archives include the exporting
  instance's ID and are rejected by any other instance, so exports are
  for archival and backup — not for migrating users to a new klangk
  deployment.
- **Know where the rest lives.** Everything else — the database
  (accounts, password history, tokens, workspace metadata) and all
  workspace home directories — is under `data_dir`
  ([Configuration File](../reference/klangkd-config.md)): the
  `klangk-data` volume (or host directory) in
  [Docker deployments](docker.md), `<state_dir>/data` for a
  [packaged `klangkd`](packaged.md).
- **Tell users the export path.** Users cannot export their own
  workspaces; an admin must do it for them. One approach: collect
  requests until the deadline, then run `klangk export` per workspace.

## Shut down cleanly

Stop the instance gracefully — do not just destroy the container or
kill the host. On SIGINT/SIGTERM the backend stops and **removes all
workspace containers** before exiting (see
[Process Signals](signals.md)), so nothing is left running on the host:

```bash
# Docker deployment
docker stop klangk

# Packaged / systemd deployment
systemctl stop klangkd
```

## After shutdown: dispose

Work through these in order:

1. **Archive or delete `data_dir` — as decided with the other admins.**
   The database contains password hashes and active tokens; treat an
   archived `data_dir` like any other credential store (encrypt the
   archive, restrict access). To destroy it instead:

   ```bash
   # Docker deployment
   docker rm klangk
   docker volume rm klangk-data      # or delete the host directory you mounted

   # Packaged deployment
   rm -rf "$XDG_STATE_HOME/klangkd"  # or your configured state_dir
   ```

2. **Dispose of secrets from the instance's configuration.** The
   `jwt_secret` value, LLM provider API keys (`KLANGKD_LLM_MODELS`),
   OIDC client secrets, and SMTP credentials. Revoking the JWT secret
   also invalidates every outstanding token, so any lingering CLI
   login or API token is dead even if someone saved it.

3. **Remove the network entries** agreed with the infrastructure
   owners: DNS record, reverse-proxy route, Tailscale ACLs, published
   ports.

## Checklist

- [ ] Shutdown date announced to users, admins, and integrators
- [ ] Workspaces that must survive exported (owners, or granted admins, run `klangk export`)
- [ ] Graceful stop performed; no workspace containers remain on the host
- [ ] `data_dir` archived (encrypted) or deleted, per the admins' decision
- [ ] Secrets revoked: JWT secret, LLM API keys, OIDC client secrets, SMTP credentials
- [ ] DNS / reverse proxy / Tailscale / firewall entries removed
