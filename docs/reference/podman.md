# Podman

!!! note
This page covers **local development** only. The published host
container image ships podman and its own storage config — no setup
needed for production deployments.

## Installation

Podman is provided by the devenv shell on all platforms — no manual
installation needed. All `podman` commands on this page assume you are
inside the devenv shell (`devenv shell`).

## Storage

Klangk uses rootless podman with `--userns=keep-id` to run workspace
containers. This maps the host user's UID into the container so files
on bind mounts are owned correctly.

When `--userns=keep-id` is used, podman must remap UIDs/GIDs in every
image layer. It does this via `storage-chown-by-maps` — a recursive
chown of the layer tree. This is slow on first container creation
(10-30s depending on image size) but subsequent creates reuse cached
layers and are fast.

### A note on "Supports shifting"

Running `podman info | grep "Supports shifting"` shows whether the
kernel's idmapped mount feature is available, which would skip the
recursive chown entirely. In practice, **this is always `false` for
rootless podman** — the kernel requires `CAP_SYS_ADMIN` in the
initial mount namespace to use `mount_setattr` with `MOUNT_ATTR_IDMAP`,
which rootless users don't have. Only rootful (`sudo`) podman can
achieve `Supports shifting: true`.

This means `storage-chown-by-maps` is the normal and expected path
for rootless podman.

## Configuring Storage

By default, podman stores images and runtime state under
`~/.local/share/containers/`. To use a different location (e.g. a
larger volume), two options:

**Option A: `KLANGK_PODMAN_STORAGE` env var** (devenv only)

Set `KLANGK_PODMAN_STORAGE` in your `.env` file:

```sh
KLANGK_PODMAN_STORAGE=/path/to/podman
```

The devenv shell generates a `storage.conf` that puts both `graphroot`
and `runroot` under this path. This only applies inside the devenv
shell — podman outside of devenv is unaffected.

**Option B: `~/.config/containers/storage.conf`** (system-wide)

Create or edit `~/.config/containers/storage.conf`:

```toml
[storage]
driver = "overlay"
graphroot = "/path/to/podman/storage"
runroot = "/path/to/podman/run"
```

This applies to all rootless podman usage, not just Klangk.

### NixOS

Klangk runs **rootless** podman. The NixOS module
`virtualisation.containers.storage.settings` writes to
`/etc/containers/storage.conf`, which only applies to **root** podman.
Rootless podman ignores it and uses `~/.config/containers/storage.conf`
instead. Create that file manually (as shown above) or use home-manager.

### macOS

No storage configuration needed. Podman runs in a Linux VM with its
own ext4 storage.

## Resetting After Config Changes

Podman persists storage paths in a SQLite database (`db.sql`) on first
run. Changing `storage.conf` alone is not enough — podman ignores
config changes if the database already exists. After changing storage
paths:

```sh
podman system reset --force
```

This removes all containers, images, and volumes. Rebuild the workspace
image afterward with `devenv up`.

## Troubleshooting

### Container creation is slow

First container creation with `--userns=keep-id` is always slow
(10-30s) due to `storage-chown-by-maps`. This is normal for rootless
podman. Subsequent creates reuse cached layers and are fast.

### "database run root does not match our run root"

Podman's database has a cached path that doesn't match your config.
Run `podman system reset --force`.

### "Found incomplete layer, deleting it"

Storage was corrupted by a prior interrupted operation (e.g., a
container create that was killed mid-write). Run:

```sh
podman system check --repair
```

Or if that hangs too, `podman system reset --force`.
