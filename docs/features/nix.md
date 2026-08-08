# Nix workspace feature

Opt-in per-workspace [nix](https://nixos.org/) + [devenv](https://devenv.sh/),
shared across workspaces without baking the ~1–2 GB nix store into every
workspace image. Tracked under [#2198](https://github.com/mcdonc/klangk/issues/2198);
this page covers the **shared base store** ([#2200](https://github.com/mcdonc/klangk/issues/2200))
that the per-workspace mount ([#2201](https://github.com/mcdonc/klangk/issues/2201))
layers on top of.

## Shared base `/nix` store (the seed)

A single, self-consistent `/nix` tree containing nix, devenv, and a matching
nix database (`/nix/var/nix/db/db.sqlite`). It is built once and shared by
every nix-enabled workspace, so the store is paid for once, not per workspace.

The seed is **not** a container image — it is a host-side tree deployed
alongside klangk (per [#2198](https://github.com/mcdonc/klangk/issues/2198): the
store is built/populated as part of the devenv setup, not baked into an image).

### Build it

```sh
devenv shell -- build-nix-seed [out-dir]
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
snapshots built on an older seed. To update (new nix/devenv, or extra base
packages):

1. _(Optional)_ edit `src/containers/nix-seed/Dockerfile` to add base packages
   (another `nix profile install nixpkgs#<pkg>`), or rely on its `latest`
   devenv/nix.
2. Rebuild the seed tree. Pass `--no-cache` to force fresh nix/devenv
   (otherwise cached layers reproduce the previous versions):

   ```sh
   devenv shell -- build-nix-seed /tmp/nix-base --no-cache
   ```

3. Reload it into the btrfs subvolume (the loader refuses to clobber, so delete
   the old seed first):

   ```sh
   btrfs subvolume delete /steam2/btrfs/klangk-nix/seed
   scripts/load-nix-seed-btrfs.sh /tmp/nix-base /steam2/btrfs/klangk-nix
   ```

New workspaces pick up the new seed on next start. **Existing workspaces keep
their snapshot** — btrfs snapshots are independent CoW copies, so they're
unaffected by the seed change; delete + recreate a workspace to give it the new
seed.

See [#2198](https://github.com/mcdonc/klangk/issues/2198) (Design section) for the
investigation that compared overlay / hardlinks / zfs / btrfs and settled on
btrfs.

## How workspaces consume it (#2201, #2208)

Each nix-enabled workspace gets a writable, isolated `/nix` as a **btrfs
snapshot** of the seed subvolume — instant, copy-on-write (only the
workspace's changes consume space), and fully isolated. klangkd snapshots the
seed on first start, reuses the snapshot across restarts, and deletes it on
workspace delete. The snapshot's `/nix` and `nix.conf` are bind-mounted into
the container.

A btrfs snapshot is reachable through the parent mount (no separate mount),
and btrfs lets a non-root user snapshot a subvolume it can write to — so this
needs **no privileged helper**. That's the deciding advantage over zfs, whose
non-root mount is impossible on Linux ([openzfs/zfs#10648](https://github.com/openzfs/zfs/discussions/10648))
and would force a `cap_sys_admin` helper (#2210). (The #2201 spike, PR #2205,
compared the options; #2210 is closed as not-needed for the btrfs path.)

### Enable it

1. Build the seed (#2200) and load it into a btrfs subvolume:

   ```sh
   devenv shell -- build-nix-seed /tmp/nix-base
   scripts/load-nix-seed-btrfs.sh /tmp/nix-base /steam2/btrfs/klangk-nix
   ```

   `/steam2/btrfs` must be a btrfs filesystem **mounted with
   `user_subvol_rm_allowed`** and writable by the klangkd user (so the same
   non-root user can snapshot _and_ delete). On NixOS, set
   `fileSystems."/steam2/btrfs".options = [ "user_subvol_rm_allowed" ];`.

2. Point klangkd at the seed subvolume (in `klangkd.yaml` or env):

   ```yaml
   nix_btrfs_subvolume: /steam2/btrfs/klangk-nix/seed
   ```

   This advertises nix availability (the create-workspace dialog shows a
   "Nix" checkbox). It does **not** force any image — image selection stays
   the user's.

3. Per workspace, tick **Nix** when creating it (or set `nix: true` in its
   settings via the API). That workspace then gets a `/nix` snapshot on start.

Workspaces without the `nix` setting are untouched. Without
`nix_btrfs_subvolume`, the checkbox is hidden and nix is image-only: pick the
nix image (`klangk-workspace-nix`) for its baked `/nix` (the non-btrfs fallback).

nix/devenv are on `$PATH` by default: klangkd sets `KLANGKWS_NIX=1`, and the
default workspace image's `/etc/profile.d/z-klangk-nix.sh` (baked in #2199)
sources nix's activation in any login shell — so `nix`/`devenv` work in any
image with no manual step. (The flag is orthogonal to image selection.)

### Restart persistence

The snapshot is a btrfs subvolume, so it survives container stop/start —
packages a user installs persist across restarts. Deleting the workspace (not
just stopping it) deletes the snapshot. Updating the seed (re-run
`build-nix-seed` + `load-nix-seed-btrfs.sh`) does not affect existing
snapshots — btrfs snapshots are independent CoW copies, so they keep their
data; new workspaces get the new seed.
