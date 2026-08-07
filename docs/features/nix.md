# Nix workspace feature

Opt-in per-workspace [nix](https://nixos.org/) + [devenv](https://devenv.sh/),
shared across workspaces without baking the ~1–2 GB nix store into every
workspace image. Tracked under [#2198](https://github.com/mcdonc/klangk/issues/2198);
this page covers the **shared base store** ([#2200](https://github.com/mcdonc/klangk/issues/2200))
that the per-workspace mount ([#2201](https://github.com/mcdonc/klangk/issues/2201))
layers on top of.

## Shared base `/nix` store (the seed)

A single, read-only, self-consistent `/nix` tree containing nix, devenv, and a
matching nix database (`/nix/var/nix/db/db.sqlite`). It is built once and
shared by every nix-enabled workspace, so the store is paid for once, not per
workspace.

The seed is **not** a container image — it is a host-side tree deployed
alongside klangk (per [#2198](https://github.com/mcdonc/klangk/issues/2198): the
store is built/populated as part of the devenv setup, not baked into an image).

### Build it

```sh
devenv run build-nix-seed [out-dir]
```

`scripts/build-nix-seed.sh` builds a throwaway sandbox image that performs a
single-user nix install + devenv, then extracts `/nix` and `/etc/nix/nix.conf`
into a deployable tree at `out-dir` (default `./nix-base`, or
`$KLANGKD_NIX_SEED_DIR`). Output layout:

```text
<out>/
├── nix/          store + var/db + base profile (nix, devenv, cachix)
└── nix.conf      flakes/nix-command + pre-configured binary caches
```

The script verifies nix and devenv run against the extracted store before
reporting success.

### Update it

The seed only ever **grows** — nix store paths are content-addressed, so
adding packages to a new seed never conflicts with existing per-workspace
layers built on an older seed. To update (new nix, new devenv), just re-run
the build and redeploy the tree. See the [overlay architecture note](../architecture/nix-workspace-overlay.md)
for how #2201 consumes an updated seed (re-clone for the zfs path; the DB is
per-clone, so existing workspaces keep their view until re-cloned).

## How workspaces consume it (#2201)

Each nix-enabled workspace gets a writable, isolated `/nix` as a **zfs clone**
of the seed snapshot — instant (~0.5 s), block-shared (only the workspace's
changes consume space), and fully isolated from other workspaces. klangkd
clones the seed on first start, reuses the clone across restarts, and destroys
it on workspace delete. The clone's `/nix` and `nix.conf` are bind-mounted
into the container.

This replaces the overlayfs approach the issue body described — the #2201
spike (PR #2205) found rootless podman rejects `--mount type=overlay`, and
in-container overlay needs single-bind + `--cap-add SYS_ADMIN`; a zfs clone is
plain-bind (no extra caps), faster, and the nix-DB copy-up gotcha doesn't
apply (a clone is a real filesystem copy). zfs is required (a btrfs-snapshot
variant is future work for non-zfs hosts).

### Enable it

1. Build the seed (#2200) and load it into a zfs dataset:

   ```sh
   devenv run build-nix-seed /tmp/nix-base
   scripts/load-nix-seed-zfs.sh /tmp/nix-base d/klangk-nix
   ```

2. Delegate zfs clone/destroy to the klangkd user (no full root):

   ```sh
   sudo zfs allow <klangkd-user> create,clone,destroy,mount d/klangk-nix
   ```

3. Point klangkd at the dataset (in `klangkd.yaml` or env):

   ```yaml
   nix_zfs_dataset: d/klangk-nix
   ```

   This advertises nix availability (the create-workspace dialog shows a
   "Nix" checkbox). It does **not** force any image — image selection stays
   the user's.

4. Per workspace, tick **Nix** when creating it (or set `nix: true` in its
   settings via the API). That workspace then gets a `/nix` clone on start.

Workspaces without the `nix` setting are untouched (no `/nix` clone). On a
host without `nix_zfs_dataset`, the checkbox is hidden and nix is
image-only: pick the nix image (`klangk-workspace-nix`) for its baked `/nix`
— the non-zfs fallback (tracked: btrfs snapshot support is #2208).

nix/devenv are off `$PATH` by default; the workspace image (#2199) ships
`/opt/klangk/bin/nix-activate.sh`, which a user sources to put nix and
nix-installed programs on `$PATH`. (A custom image + the nix flag + zfs also
works — `/nix` comes from the clone; the user activates it themselves if
their image lacks the script.)

### Restart persistence

The clone is a zfs dataset, so it survives container stop/start — packages a
user installs persist across restarts. Destroying the workspace (delete, not
stop) destroys the clone. Updating the seed (re-run `build-nix-seed` +
`load-nix-seed-zfs.sh`) does not affect existing clones; new workspaces get
the new seed. (The seed snapshot is immutable; reseeding requires `zfs destroy
-r <parent>/seed` first, which orphans existing clones — re-create workspaces
or point them at the new seed.)
