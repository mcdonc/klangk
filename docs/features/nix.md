# Nix workspace feature

Opt-in per-workspace [nix](https://nixos.org/) + [devenv](https://devenv.sh/),
shared across workspaces without baking the ~1–2 GB nix store into every
workspace image.

> **Off by default (#2560).** The whole feature is gated by the
> `nix_enabled` master switch (default `false`). While off, the nix toggle
> is absent from all create/edit surfaces (web, TUI, CLI), the API rejects
> a new `nix: true` opt-in with a clear error, and a workspace start with a
> stored nix flag proceeds without the `/nix` mount (logged once at info).
> Arm it with:
>
> ```yaml
> nix_enabled: true # or env KLANGKD_NIX_ENABLED=1
> ```
>
> Reloadable on SIGHUP. Deleting a workspace still tears down its
> per-workspace layer while the switch is off; re-enabling resumes the
> mount (the layers persist).

A single shared **base `/nix` store** (the _seed_) is built once and layered
per-workspace by one of two backends:

- **`fuse-overlayfs`** (the default) — a `fuse-overlayfs` overlay of a plain
  directory seed; works on any filesystem.
- **`btrfs-snapshot`** — a CoW snapshot of a btrfs subvolume seed; the
  space-optimised choice where btrfs is available.

Neither needs a privileged helper.

## Build the seed

```sh
klangk-build-nix-seed [out-dir]   # default ./nix-base, or $KLANGKBUILD_NIX_SEED_DIR
```

`klangk-build-nix-seed` builds a throwaway sandbox image that performs
a single-user nix install + devenv, then extracts `/nix` and `/etc/nix/nix.conf`
into a deployable tree. It ships the seed Dockerfile inside the wheel
(`importlib.resources`, with a source-tree fallback in dev) and drives the
**configured** podman (`KLANGKD_PODMAN_BIN`, the same binary the server uses),
so it works identically from a `pip install klangk` deployment (host container,
bare pip) and a devenv shell. Output:

```text
<out>/
├── nix/          store + var/db + base profile (nix, devenv, cachix)
└── nix.conf      flakes/nix-command + pre-configured binary caches
```

It verifies `nix --version` + `devenv --version` run against the extracted
store before reporting success. This build output is what both backends
consume below.

## Consume it per-workspace

Each nix-enabled workspace gets a writable, isolated `/nix` derived from the
seed. klangkd provisions it on first start, reuses it across restarts, and
deletes it on workspace delete. Configure the seed + backend with one block —
`nix_seed` — in `klangkd.yaml` (or env `KLANGKD_NIX_SEED__TYPE` /
`KLANGKD_NIX_SEED__PATH`):

```yaml
nix_seed:
  type: btrfs-snapshot # or fuse-overlayfs (the default)
  path: /path/to/seed
```

Omit `nix_seed` (or leave `path` unset) to disable the feature — no
per-workspace `/nix` is provisioned.

### `fuse-overlayfs` (the default — no btrfs)

The seed is the plain `klangk-build-nix-seed` output directory — **no prepare
step**. Each workspace gets a `fuse-overlayfs` overlay: the seed is the
read-only lower layer; a per-workspace upper layer captures writes (new store
paths, profile/db updates). Works on **any filesystem**; per-workspace overlays
land as siblings of the seed dir (`<seed parent>/ws-<id>`). Needs
`fuse-overlayfs` + `fusermount3` + a writable `/dev/fuse` (on NixOS,
`fuse-overlayfs` is in the dev shell, `fusermount3` ships as a setuid wrapper,
`/dev/fuse` is world-rw by default).

```sh
klangk-build-nix-seed /var/lib/klangk/nix-base
```

```yaml
nix_seed:
  path: /var/lib/klangk/nix-base # type defaults to fuse-overlayfs
```

> **Where it works.** `fuse-overlayfs` suits a **bare-metal Linux** host
> (podman runs on the host — no userns nesting between klangkd's FUSE mount and
> the workspace container). It does **not** work where podman is nested — the
> rootless runtime can't bind a process-owned FUSE mount into a workspace
> container's userns (host-container and macOS deployments). Use
> `btrfs-snapshot` there if you have btrfs.

### `btrfs-snapshot` (CoW — needs btrfs)

The seed is a btrfs subvolume; each workspace gets a CoW snapshot (instant;
only the workspace's changes consume space). btrfs lets a non-root user
snapshot a subvolume it can write to, so this needs **no privileged helper**.
Requires a btrfs filesystem mounted with `user_subvol_rm_allowed`, writable by
the klangkd user (so the same non-root user can snapshot _and_ delete). On
NixOS: `fileSystems."/steam2/btrfs".options = [ "user_subvol_rm_allowed" ];`.

Load the build output into a subvolume, then point `nix_seed` at it:

```sh
klangk-build-nix-seed /tmp/nix-base
klangk-load-nix-seed-btrfs /tmp/nix-base /steam2/btrfs/klangk-nix
# → creates the subvolume /steam2/btrfs/klangk-nix/seed
```

```yaml
nix_seed:
  type: btrfs-snapshot
  path: /steam2/btrfs/klangk-nix/seed
```

A `btrfs-snapshot` seed whose `path` isn't on btrfs fails fast at startup with
a clear error.

### Enable nix on a workspace

Per workspace, tick **Nix** when creating it (or set `nix: true` via the API).
That workspace then gets a `/nix` on start. Image selection stays the user's
choice — the flag never forces an image. With the feature armed
(`nix_enabled` on + `nix_seed` configured, #2560), the create-workspace
dialog shows a "Nix" checkbox; otherwise the checkbox is hidden and the API
rejects a new `nix: true` (an edit form echoing an already-stored value is
tolerated).

nix/devenv are on `$PATH` by default: klangkd sets `KLANGKWS_NIX=1`, and the
default workspace image's `/etc/profile.d/z-klangk-nix.sh` sources
nix's activation in any login shell — so `nix`/`devenv` work in any image with
no manual step.

## Update the seed

The seed only ever **grows** — nix store paths are content-addressed, so a new
seed never conflicts with existing per-workspace layers. To update (new
nix/devenv, or extra base packages):

1. _(Optional)_ edit `src/containers/nix-seed/Dockerfile` to add base packages
   (another `nix profile install nixpkgs#<pkg>`), or rely on its `latest`
   devenv/nix.
2. Rebuild in place. `--no-cache` forces fresh nix/devenv (otherwise cached
   layers reproduce the previous versions):

   ```sh
   klangk-build-nix-seed --update --no-cache /var/lib/klangk/nix-base
   ```

3. **`btrfs-snapshot` only** — reload the subvolume (the loader refuses to
   clobber, so delete the old one first):

   ```sh
   btrfs subvolume delete /steam2/btrfs/klangk-nix/seed
   klangk-load-nix-seed-btrfs /var/lib/klangk/nix-base /steam2/btrfs/klangk-nix
   ```

   **`fuse-overlayfs`** needs nothing more — `nix_seed.path` points at the dir,
   so new workspaces pick up the rebuilt seed on next start.

New workspaces pick up the new seed on next start. **Existing workspaces keep
their `/nix`** — btrfs snapshots are independent CoW copies, and fuse-overlayfs
uppers carry their own copy-ups — so they keep installed packages; delete +
recreate a workspace to give it the new seed.

## Persistence

The per-workspace `/nix` survives container stop/start — packages a user
installs persist across restarts. Deleting the workspace (not just stopping
it) tears it down (btrfs subvolume delete, or `fusermount3 -u` + remove the
overlay).

One difference across backends on a **klangkd restart**: a btrfs snapshot is
just a directory reachable through the parent mount, so it survives; a
fuse-overlayfs mount is a FUSE handle owned by the klangkd process, so it does
not — klangkd re-mounts it on the workspace's next start (the upper dir +
installed packages persist, only the mount is re-created).

---

Neither backend needs a privileged helper — the deciding advantage over zfs,
whose non-root mount is impossible on Linux and would force a
`cap_sys_admin` helper.
