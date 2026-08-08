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
`$KLANGK_NIX_SEED_OUT`). Output layout:

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

## How workspaces consume it (#2201, #2208, #2219, #2220)

Each nix-enabled workspace gets a writable, isolated `/nix` derived from the
shared seed. klangkd provisions it on first start, reuses it across restarts,
and deletes it on workspace delete; the `/nix` and `nix.conf` are bind-mounted
into the container. Configure it with one block — `nix_seed` — grouping the
seed path and the backend that consumes it:

```yaml
nix_seed:
  type: btrfs-snapshot # or fuse-overlayfs (the default)
  path: /path/to/seed
```

Omit `nix_seed` entirely (or leave `path` unset) to disable the feature — nix
is then image-only (pick the nix image `klangk-workspace-nix` for its baked
`/nix`). The two backends:

- **`btrfs-snapshot`** — the seed is a btrfs subvolume; each workspace gets a
  CoW snapshot (instant, only the workspace's changes consume space). A
  snapshot is reachable through the parent mount and btrfs lets a non-root
  user snapshot a subvolume it can write to, so this needs **no privileged
  helper**. Requires a btrfs filesystem mounted with `user_subvol_rm_allowed`.
  The CoW-optimised choice where btrfs is available.
- **`fuse-overlayfs`** (the default) — the seed is a plain directory (the
  `build-nix-seed` output, on any filesystem); each workspace gets a
  `fuse-overlayfs` overlay with the seed as the read-only lower layer and a
  per-workspace upper layer that captures writes (new store paths, profile/db
  updates). Also **no privileged helper** — needs `fuse-overlayfs` +
  `fusermount3` + `/dev/fuse`.

Both need no privileged helper — that's the deciding advantage over zfs, whose
non-root mount is impossible on Linux ([openzfs/zfs#10648](https://github.com/openzfs/zfs/discussions/10648))
and would force a `cap_sys_admin` helper (#2210). (The #2201 spike, PR #2205,
compared the options; #2210 is closed as not-needed.)

> **Where the fuse backend works.** `fuse-overlayfs` suits a bare-metal Linux
> host (podman runs on the host — no userns nesting between klangkd's FUSE
> mount and the workspace container). It does **not** work where podman is
> nested — the rootless runtime can't bind a process-owned FUSE mount into a
> workspace container's userns (host-container and macOS deployments; see
> #2221). Use `btrfs-snapshot` there if you have btrfs, otherwise the nix
> image.

### Enable it

1. Build the seed (#2200):

   ```sh
   devenv shell -- build-nix-seed /tmp/nix-base
   ```

2. Pick a backend, prepare the seed for it, and set `nix_seed` (in
   `klangkd.yaml`, or env `KLANGKD_NIX_SEED__TYPE` / `KLANGKD_NIX_SEED__PATH`):
   - **`btrfs-snapshot`** — load the seed into a subvolume:

     ```sh
     scripts/load-nix-seed-btrfs.sh /tmp/nix-base /steam2/btrfs/klangk-nix
     ```

     `/steam2/btrfs` must be a btrfs filesystem **mounted with
     `user_subvol_rm_allowed`** and writable by the klangkd user (so the same
     non-root user can snapshot _and_ delete). On NixOS:
     `fileSystems."/steam2/btrfs".options = [ "user_subvol_rm_allowed" ];`.

     ```yaml
     nix_seed:
       type: btrfs-snapshot
       path: /steam2/btrfs/klangk-nix/seed
     ```

   - **`fuse-overlayfs`** (no btrfs) — use the seed directory directly (no
     loader step):

     ```yaml
     nix_seed:
       path: /var/lib/klangk/nix-base # type defaults to fuse-overlayfs
     ```

     The host needs `fuse-overlayfs`, `fusermount3`, and a writable
     `/dev/fuse` (on NixOS, `fuse-overlayfs` is in the dev shell; `fusermount3`
     ships as a setuid wrapper; `/dev/fuse` is world-rw by default). Per-workspace
     overlays land as siblings of the seed dir (`<seed parent>/ws-<id>`).

   Either backend advertises nix availability (the create-workspace dialog
   shows a "Nix" checkbox). It does **not** force any image — image selection
   stays the user's. A `btrfs-snapshot` seed whose path isn't on a btrfs
   filesystem fails fast at startup with a clear error.

3. Per workspace, tick **Nix** when creating it (or set `nix: true` in its
   settings via the API). That workspace then gets a `/nix` on start.

Workspaces without the `nix` setting are untouched. With `nix_seed` unset, the
checkbox is hidden and nix is image-only.

nix/devenv are on `$PATH` by default: klangkd sets `KLANGKWS_NIX=1`, and the
default workspace image's `/etc/profile.d/z-klangk-nix.sh` (baked in #2199)
sources nix's activation in any login shell — so `nix`/`devenv` work in any
image with no manual step. (The flag is orthogonal to image selection.)

### Restart persistence

The per-workspace `/nix` survives container stop/start — packages a user
installs persist across restarts. Deleting the workspace (not just stopping
it) tears it down (btrfs subvolume delete, or `fusermount3 -u` + remove the
overlay). Updating the seed does not affect existing per-workspace `/nix` —
btrfs snapshots are independent CoW copies, and fuse-overlayfs uppers carry
their own copy-up of anything changed — so they keep their data; new
workspaces get the new seed.

One difference across backends on a **klangkd restart**: a btrfs snapshot is
just a directory reachable through the parent mount, so it is still there;
a fuse-overlayfs mount is a FUSE handle owned by the klangkd process, so it
does not survive the process — klangkd re-mounts it on the workspace's next
start (the upper dir + installed packages persist, only the mount is
re-created).
