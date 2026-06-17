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

## Idmapped Mounts

For fast container creation, podman needs **idmapped mount** support
from the kernel and filesystem. This lets podman remap UIDs at the
mount level instead of recursively chowning every file in every image
layer (the `storage-chown-by-maps` fallback, which can take minutes
or hang).

Check your setup:

```sh
podman info | grep "Supports shifting"
```

- **`true`** — idmapped mounts work. Container creation is fast (< 2s).
- **`false`** — podman falls back to `storage-chown-by-maps`. The first
  container create with `--userns=keep-id` will be slow (20-30s) or may
  hang entirely.

## Filesystem Requirements

Idmapped mounts require the **graphroot** (image layers) and **runroot**
(runtime state) to both be on a filesystem that supports them:

| Filesystem | Idmapped mounts | Notes                            |
| ---------- | --------------- | -------------------------------- |
| ext4       | Yes             | Recommended                      |
| btrfs      | Yes             | Works                            |
| XFS        | Yes             | Works                            |
| ZFS        | No              | Hangs on `storage-chown-by-maps` |
| tmpfs      | Yes             | Fine for runroot                 |

If your home directory is on ZFS (common on NixOS), podman's default
storage paths (`~/.local/share/containers/`) will be on ZFS and
idmapped mounts won't work.

## Configuring Storage

To move podman storage to a supported filesystem, create or edit
`~/.config/containers/storage.conf`:

```toml
[storage]
driver = "overlay"
graphroot = "/path/to/ext4/podman/storage"
runroot = "/path/to/ext4/podman/run"
```

Both `graphroot` and `runroot` must be on a supported filesystem.

### NixOS

On NixOS, configure via your host's nix config:

```nix
virtualisation.containers.storage.settings = {
  storage = {
    driver = "overlay";
    graphroot = "/path/to/ext4/podman/storage";
    runroot = "/path/to/ext4/podman/run";
  };
};
```

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

### Container creation hangs or times out

Check `podman info | grep "Supports shifting"`. If `false`, your
storage is on an unsupported filesystem. See [Configuring Storage](#configuring-storage).

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
