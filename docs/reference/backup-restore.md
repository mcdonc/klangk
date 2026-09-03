# Backup and Restore

This chapter is for operators who need to **move a klangk site to new hardware**
or **recover after a host failure**: back everything up on the old host, restore
it on the new one, and end up with a site where every user, workspace, and file
is intact.

It covers full-site backup only. For single-workspace archival see
[Workspace Export & Import](../features/export-import.md) — but note exports are
**same-instance-only** (an archive carries the exporting instance's ID and is
rejected by any other instance), so per-workspace export complements this
chapter, it does not replace it. For retiring a site permanently, see
[Decommissioning](../deployment/decommissioning.md).

!!! note
Automated, scheduled snapshots of the data dir are not available yet.
When that feature lands it will cover the data-dir half of this procedure; the podman
volume half (below) stays manual either way.

## Where klangk state lives

A restorable backup must capture **all** of the items in this table. Miss one
and some part of the site does not come back.

| State                             | Where it lives                                                                                          | What breaks if lost                                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| **Data dir** (`KLANGKD_DATA_DIR`) | `<state_dir>/data` (default `~/.local/state/klangkd/data`); `/home/klangk/data` in the Docker image     | Users, workspaces, ACLs, sessions — and every workspace home directory           |
| **Podman named volumes**          | The podman storage of the account klangkd runs podman as (labeled `klangk.managed=true`)                | Contents of every user-mounted named volume                                      |
| **Configuration**                 | Unit file / env file **and** the [config file](klangkd-config.md) (`~/.config/klangkd/klangkd.yaml`)    | Site fails to boot or boots with wrong settings; JWT secret loss forces re-login |
| **Customization**                 | `KLANGKD_CUSTOMIZE_DIR` tree, `oidc.yaml`, hook files (see [Customizing](../deployment/customizing.md)) | Branding, email templates, private-CA trust, OIDC login                          |
| **Host bind-mount sources**       | Host directories mounted into workspaces via `extra_mounts` (e.g. `/home/klangk/mount`)                 | Workspaces using them refuse to start                                            |
| **Per-workspace nix layers**      | `<nix seed parent>/ws-<workspace id>` — **only if you enabled the nix feature**                         | Workspaces lose nix-installed packages                                           |
| **Container images**              | Local podman image store                                                                                | Workspace / sidecar images must be re-pulled or rebuilt                          |

### The data dir

The data dir is the single most important artifact. It holds:

- `klangk.db` — the SQLite database: users, password history, sessions, API
  tokens, workspaces, ACL entries, port allocations.
- `instance-id` — the site's instance identity (see
  [Instance identity](#instance-identity-the-instance-id-file) below).
- `workspaces/<workspace id>/home` — **every workspace's home directory**.
  Homes are plain directories bind-mounted into containers as `/home`; they are
  _not_ podman volumes.
- `workspaces/<workspace id>/config` — per-workspace config directories.
- `branding/` — fallback branding location, used by deployments that place
  branding under the data dir instead of the customize dir
  (see [Customizing](../deployment/customizing.md)).

### Podman named volumes

Named volumes are the volumes users can attach to workspaces (created from the
UI, the `klangk volumes create` CLI command, or a workspace mount spec like
`mydata:/data`). Every one klangk manages carries two labels, and a third
when a specific user owns it:

```text
klangk.managed=true      managed by klangk (never touched by other tooling)
klangk.instance=<id>     which klangk site owns it
klangk.user-id=<id>      which user owns it (volumes created via the API)
```

The labels are not decoration: the volume listing (`klangk volumes ls`, the
workspace editor's volume picker) filters on `klangk.instance` +
`klangk.user-id`, and starting a workspace whose volume carries a different
instance label is refused. **A restored volume without its original labels is
invisible or rejected.** The procedures below preserve them.

!!! warning
Do **not** back volumes up by copying
`~/.local/share/containers/storage/volumes` directly. That path is
storage-driver-specific (the on-disk layout differs between overlay, btrfs,
and zfs drivers), it embeds UID mappings that differ between rootless
accounts, and copying it is unsafe unless podman is fully stopped. Use
`podman volume export` / `podman volume import` (driver-independent) as
shown below.

### Configuration

Everything the operator configured outside the data dir:

- The `KLANGKD_*` environment (unit file `Environment=` lines, `.env` file, or
  `docker run -e ...` flags) — including `KLANGKD_JWT_SECRET` and
  `KLANGKD_LLM_MODELS`.
- The [config file](klangkd-config.md), if used: `~/.config/klangkd/klangkd.yaml`
  by default, or whatever path `--config` names.
- Any **`file:`-referenced secret files** (e.g. `KLANGKD_JWT_SECRET=file:/run/secrets/jwt`).
  These live on disk; the env var only names them.

!!! note
**JWT secret continuity.** Restore the same `KLANGKD_JWT_SECRET` (or its
`file:` source). A different secret does not corrupt anything, but every
issued token stops verifying and all users must log in again.

### Customization

Everything covered in [Customizing a Deployment](../deployment/customizing.md):

- The whole `KLANGKD_CUSTOMIZE_DIR` tree — `branding/`, `certs/`,
  `email-templates/`.
- The OIDC config file (`KLANGKD_OIDC_CONFIG`, e.g. `oidc.yaml`).
- The OIDC login hook (`KLANGKD_OIDC_LOGIN_HOOK`) and the workspace-created
  hook (`KLANGKD_WORKSPACE_CREATED_HOOK`) if they are files bind-mounted from
  outside the customize dir.

Losing these does not stop the site from booting, but it reverts branding and
email templates, un-trusts private CAs (workspace `git clone` over TLS to
internal hosts fails), and breaks OIDC login.

### Host bind-mount sources

A workspace mount whose source contains a `/` is a **host bind mount** (for
example `-v ./mount:/home/klangk/mount` in the Docker deployment, then
`/home/klangk/mount/project:/src` on a workspace). The database stores the mount
_spec_, but the directory _contents_ live wherever the operator put them —
outside the data dir and outside podman volumes. A workspace whose bind source
is missing refuses to start (`Bind mount source does not exist`).

If your deployment uses host bind mounts, add each source tree to the backup.

### Per-workspace nix layers

!!! note
**Skip this unless you enabled the nix feature.** Most deployments have not:
per-workspace `/nix` requires `nix_seed.path` **and** `nix_enabled` in the
configuration (see [Nix](../features/nix.md)). If you did not set both,
there is nothing nix-related to back up.

If the feature is enabled, each nix-enabled workspace has a writable `/nix`
layer at `<nix seed parent>/ws-<workspace id>` (a btrfs snapshot or a
fuse-overlayfs upper dir — outside the data dir). Back up:

- the `ws-*` trees — a workspace that loses its layer re-provisions a fresh
  empty one from the seed, but loses every nix-installed package/profile;
- the seed itself — it is operator-built, not re-downloadable.

### Container images

- **Registry-referenced images** (fully-qualified references like
  `ghcr.io/mcdonc/klangk/klangk-workspace:v1.0`) re-pull on the new host.
  Nothing to back up.
- **Locally-built images** are in no registry. That includes a workspace image
  built from a fork, and the host image itself for feature forks. Either back
  them up with `podman save` / `podman load`, or plan to rebuild from your fork
  (see [Building a Custom Image](../deployment/customizing.md#building-a-custom-image-features)).

### What is regenerated automatically

Do not chase these after a restore — the backend recreates them at boot:

- `<state_dir>/klangk.sock` and the PID file (the proxy config itself is
  pushed to Caddy over its admin API, never written to disk).
- `<state_dir>/ssl/ca-bundle.crt` (rebuilt from the customize dir's `certs/`).
- `<data_dir>/ws-tokens/` (sidecar auth tokens).
- Workspace containers and network sidecars themselves — containers are
  re-created from database records on start. Running container state (a `tmux`
  session's scrollback, a running process) does not survive any backup.

## Backup procedure

Run the backup as the **same account klangkd runs podman as** (rootless: the
klangkd user; Docker host image: inside the container via `docker exec`). On a
rootful install, that means the same `sudo podman` store the daemon uses —
rootless and rootful podman have **separate** volume stores, so a backup taken
from the wrong one finds no volumes.

!!! note
`podman volume export` / `podman volume import` need **podman 4.2+**.

!!! tip
In the [Docker deployment](../deployment/docker.md) podman runs _inside_ the
`klangk` container. Prefix every podman command with
`docker exec klangk` (e.g. `docker exec klangk podman volume ls ...`), and
note that podman's volumes live inside the container's writable layer —
they do **not** survive `docker rm` of the container, which is exactly why
the export step below matters.

### 1. Quiesce the site

Stop klangkd gracefully — do not just power off the host. On SIGTERM klangkd
stops and removes all workspace containers first, so no process is writing
into a volume while it is being exported:

```bash
# systemd / packaged deployment — podman runs on the host
systemctl stop klangkd

# Docker deployment — podman runs INSIDE the still-running klangk container.
# docker exec needs a running container, so do NOT stop the container yet;
# stop the workspace containers inside it instead (klangkd does not restart
# them — autostart applies only at daemon boot):
docker exec klangk podman stop --all --timeout 5
```

### 2. Back up the named volumes

Save the volume **metadata** first (the labels must be recreated on restore),
then export each volume's contents:

```bash
BACKUP=/srv/klangk-backup-$(date +%Y%m%d)
mkdir -p "$BACKUP/volumes"

# Names + labels of every klangk-managed volume
podman volume ls --filter label=klangk.managed=true --format '{{.Name}}' \
  | grep . > "$BACKUP/volumes/names.txt"
if test -s "$BACKUP/volumes/names.txt"; then
  podman volume inspect $(cat "$BACKUP/volumes/names.txt") \
    > "$BACKUP/volumes/inspect.json"
else
  echo "[]" > "$BACKUP/volumes/inspect.json"   # site has no named volumes
fi

# Contents, one tarball per volume
for vol in $(cat "$BACKUP/volumes/names.txt"); do
  podman volume export "$vol" | gzip > "$BACKUP/volumes/$vol.tar.gz"
done
```

(The `grep .` and the `if` simply keep a site with no named volumes from
erroring — an empty `podman volume inspect` invocation is an error.)

### 3. Stop the rest (Docker only) and back up the data dir

In the Docker deployment, stop the container now — the volume exports above
were the last step that needed it running:

```bash
docker stop klangk   # Docker deployment only
```

The data dir must exist — a tar of an unset variable produces an empty archive
that _looks_ like a successful backup:

```bash
# systemd / packaged deployment — the data dir is a host path
DATA_DIR="${KLANGKD_DATA_DIR:-$HOME/.local/state/klangkd/data}"
test -d "$DATA_DIR" || { echo "data dir $DATA_DIR not found" >&2; exit 1; }
tar -C "$(dirname "$DATA_DIR")" -czf "$BACKUP/data-dir.tar.gz" "$(basename "$DATA_DIR")"

# Docker deployment — the data dir is the klangk-data volume; archive it
# through a throwaway container (same tarball layout: a top-level data/ member)
docker run --rm -v klangk-data:/data -v "$BACKUP:/backup" alpine \
  tar -C / -czf /backup/data-dir.tar.gz data
```

Then everything else:

```bash
# Config file + customize dir, if they exist (skip cleanly when not)
if test -d ~/.config/klangkd; then
  tar -C ~ -czf "$BACKUP/config.tar.gz" .config/klangkd
fi

# Env config: copy the unit file / .env / docker-compose.yml you deploy with
cp /etc/systemd/system/klangkd.service "$BACKUP/" 2>/dev/null || true
cp ./docker-compose.yml "$BACKUP/" 2>/dev/null || true

# Any file: secret files referenced from the environment
# (know where yours live — e.g. /run/secrets/*, ./secrets/*)

# Any host bind-mount source trees (e.g. ./mount)
# tar -C . -czf "$BACKUP/mount.tar.gz" mount

# Locally-built images, if any
# podman save localhost/klangk-workspace:custom | gzip > "$BACKUP/images.tar.gz"
```

Store the backup somewhere **off the host** — a backup that dies with the host
it backs up is not a backup.

## Restore procedure

Work in this order: configuration and customization first, data dir next,
volumes last.

### 1. Restore configuration, customization, and the data dir

Recreate the env config, config file, secret files, customize dir, and hook
files on the new host, **exactly as they were** (same `KLANGKD_JWT_SECRET`,
same paths). Then restore the data dir:

```bash
DATA_DIR="${KLANGKD_DATA_DIR:-$HOME/.local/state/klangkd/data}"
mkdir -p "$(dirname "$DATA_DIR")"
tar -C "$(dirname "$DATA_DIR")" -xzf "$BACKUP/data-dir.tar.gz"
```

!!! tip
In the Docker deployment the data dir is the `klangk-data` volume — restore
into it with a throwaway container, mirroring the backup command (both sides
use a top-level `data/` tar member, so no `--strip-components` is needed — but
do not mix this pair with the host-path form above):

```bash
docker run --rm -v klangk-data:/data -v "$BACKUP:/backup" alpine \
  tar -C / -xzf /backup/data-dir.tar.gz
```

Verify the restore landed at the volume root — `instance-id` and `klangk.db`
directly under the mount, **not** under a nested `data/` — before starting the
container. A nested restore regenerates the instance id silently and orphans
every labeled volume.

#### Instance identity: the `instance-id` file

klangk has no `KLANGKD_INSTANCE_ID` setting — a site's identity is the file
`<data_dir>/instance-id`, generated once on first boot. Because it lives inside
the data dir, restoring the data dir preserves it automatically, and the
restored volumes' `klangk.instance` labels match. **Do not** delete the file: a
regenerated instance id makes every existing labeled volume foreign, and
starting a workspace that mounts one fails with
`Volume '...' is not managed by this klangk instance`.

### 2. Recreate the named volumes with their original labels

Read the labels from the saved `inspect.json` and pass them to
`podman volume create`:

```bash
for vol in $(cat "$BACKUP/volumes/names.txt"); do
  labels=$(python3 - "$BACKUP/volumes/inspect.json" "$vol" <<'EOF'
import json, sys
vols = {v["Name"]: v for v in json.load(open(sys.argv[1]))}
for k, v in vols[sys.argv[2]].get("Labels", {}).items():
    print(f"--label={k}={v}")
EOF
)
  # shellcheck disable=SC2086
  podman volume inspect "$vol" >/dev/null 2>&1 || podman volume create $labels "$vol"
done
```

(The `inspect ||` guard skips volumes that already exist in the target podman
store — creating over an existing name is an error.)

### 3. Import the volume contents

```bash
for vol in $(cat "$BACKUP/volumes/names.txt"); do
  gunzip -c "$BACKUP/volumes/$vol.tar.gz" | podman volume import "$vol" -
done
```

### 4. Start and verify

```bash
systemctl start klangkd        # or: docker start klangk / docker compose up -d
```

Then verify, in order:

1. `GET /api/v1/version` (or the login page) responds.
2. Log in as a known user; the users list is intact.
3. Start a workspace that has files in its home; the files are there.
4. `klangk volumes ls` (as a user who owned volumes) lists the restored volumes.
5. Start a workspace that mounts a named volume; it mounts without the
   `not managed by this klangk instance` error.

## What does not survive a restore

- **Running container state** — terminal sessions, running processes, scrollback.
  Containers are re-created from database records; only what is on disk in homes
  and volumes comes back.
- **Nothing else.** With the full table above captured, the restored site is
  indistinguishable from the original — users stay logged in when the JWT
  secret was restored unchanged.
