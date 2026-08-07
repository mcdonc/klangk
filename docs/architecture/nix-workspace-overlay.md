# Nix workspace store: sharing + per-workspace isolation

Spike findings for [#2201](https://github.com/mcdonc/klangk/issues/2201)
(host-side mount for per-workspace `/nix`), part of the nix workspace feature
([#2198](https://github.com/mcdonc/klangk/issues/2198)).

## Goal

A nix-enabled workspace needs `/nix/store` populated with nix + devenv + deps,
but we do **not** want to bake that ~1–2 GB into every workspace's container
image. Instead: one shared, read-only **seed** store on the host, and each
workspace gets a writable, isolated view of it (its own installs land
separately and persist). The image stays small; the store is shared.

Three approaches were spiked on this host (Linux 6.18, podman 5.8.2, zfs
2.4.3, rootless). The decisive results and a recommendation follow.

## The rootless-overlay constraint

The first idea — `podman run --mount type=overlay,lowerdir=<seed>,upperdir=<ws>,workdir=<ws>,target=/nix/store` — is **not supported**: podman rejects it
(`invalid filesystem type "overlay"`).

The alternative is for the container to mount overlayfs itself with a
namespaced `--cap-add SYS_ADMIN`. That works rootless on kernel ≥ 5.11, **but
with a hard constraint**: `lowerdir`/`upperdir`/`workdir` must be subdirs of a
**single bind mount**. Three separate bind mounts (the natural "shared seed at
one host path, per-workspace upper/work at another" layout) fail with
`wrong fs type, bad option, bad superblock` — independent of `:ro`.

So rootless overlay is usable only if the seed and the workspace's
upper/work share one bind mount.

## Options compared

| option                   | per-ws create         | per-ws delete                        | isolation                         | mount mechanism                                    | fs requirement |
| ------------------------ | --------------------- | ------------------------------------ | --------------------------------- | -------------------------------------------------- | -------------- |
| **shared-tree overlay**  | ~free (bind existing) | ~free                                | siblings visible (gated by perms) | overlay, needs single-bind + `--cap-add SYS_ADMIN` | any            |
| **hardlinks (`cp -al`)** | ~16 s                 | painful (`chmod -R u+w` + `rm` pass) | full                              | overlay (same single-bind constraint)              | any (same fs)  |
| **zfs clone**            | **~0.5 s**            | **~1 s**                             | full                              | **plain bind** (no overlay, no extra caps)         | zfs            |
| btrfs snapshot           | instant               | instant                              | full                              | plain bind                                         | btrfs          |

### Shared-tree overlay

Bind one host tree containing both the shared seed and per-workspace
`upper`/`work` as subdirs:

```text
$KLANGKD_DATA_DIR/nix/            ← bind this WHOLE tree as ONE mount
├── seed/store/                   ← the shared seed (one copy, all workspaces)
├── ws/<id>/upper/                ← this workspace's writes
└── ws/<id>/work/
```

Validated end-to-end: nix runs against the overlaid store; `nix profile
install nixpkgs#hello` lands in `ws/<id>/upper`, the seed stays read-only.

Tradeoff: every nix container can _see_ its sibling `ws/<other-id>` dirs
(mitigated by per-dir ownership — no worse than how `/opt/klangk/config` is
already shared). Works on any filesystem.

### Hardlinks

Per-workspace staging dir = `cp -al` of the seed (hardlinks → shared inodes,
~no data dup) + fresh `upper`/`work`, bind-mounted as one tree.

- Data sharing confirmed (seed and staging files share inodes).
- **~16 s** per workspace to `cp -al` (the seed is ~187k files).
- **Deletion is the killer:** nix store files are `0444`/`0555`, so removing a
  staging dir needs a `chmod -R u+w` pass first, then `rm` — a second full
  per-file pass, and fragile (during the spike the owner could not complete
  the `rm` without `sudo`). Both create and delete carry a per-workspace tax.

### zfs clone (recommended)

One seed dataset snapshotted at `@base`; each workspace is a **clone** of that
snapshot — a writable, block-shared (copy-on-write) dataset that starts
identical to the seed. Bind-mount the clone's `/nix` into the container.

Measured on this host (seed = nix 2.35.1 + devenv 2.2.1, 1.16 GB zfs-refer):

```text
clone create      0.57 s
clone destroy     1.02 s
clone USED        1.48 MB   (REFER 1.16 GB — only the install diff is allocated)
```

- Instant create/destroy, no overlay, no `--cap-add`, plain bind mount.
- Full isolation (install landed in the clone; the seed snapshot was untouched).
- Block sharing via CoW — a workspace only pays for what it changes.

## Recommendation: zfs clone

Where a zfs pool is available for klangk's data (this host has `NIXROOT` and
`d`), **zfs clone** is strictly the best option: instant lifecycle, full
isolation, block-shared, and the simplest mount story (plain bind, no overlay,
no extra capabilities). btrfs snapshots are the equivalent on btrfs.

**Design sketch for #2201:**

- **Seed (#2200):** a zfs dataset, e.g. `tank/klangk-nix-seed`, holding a
  single-user nix `/nix` (store + var + db + base profile), snapshotted at
  `@base`. The seed only ever grows (content-addressed store paths), so the
  snapshot stays a valid lower base.
- **Per nix workspace (start):** `zfs clone tank/klangk-nix-seed@base
tank/klangk-nix-ws-<id>`, then bind-mount the clone's `/nix` into the
  container at `/nix` (one bind, alongside the existing `/home` and
  `/opt/klangk/config` binds).
- **Per nix workspace (delete):** `zfs destroy tank/klangk-nix-ws-<id>`.
- **Workspace image (#2199):** stays light — no `/nix` baked; just the
  activation glue (`/opt/klangk/bin/nix-activate.sh`) and `/etc/nix/nix.conf`.

**Privilege:** zfs clone/snapshot/destroy/mount need privilege. The clean
answer is zfs's own delegation, scoped to the nix datasets, so klangkd needs
no full root:

```sh
zfs allow <klangkd-user> create,destroy,clone,mount,snapshot tank/klangk-nix
```

A klangkd setting (e.g. `nix_zfs_dataset`) names the seed dataset parent;
absent it, klangkd falls back to the shared-tree overlay.

**DB handling:** the clone is a real filesystem copy (store + var + db), so
each workspace has its own consistent DB — the overlay copy-up-DB gotcha (a
single `db.sqlite` masked by the upper layer on first write) does not apply.
nix's experimental `local-overlay-store` (overlay `/nix/store` only, dual DB)
is the way to coordinate DBs _if_ seed updates must propagate to live
workspaces; with cheap clones, the simpler model is "re-clone on seed bump."

## Open questions for implementation

- Config surface: the `nix_zfs_dataset` setting + a feature flag to mark a
  workspace nix-enabled (the image/overlay selection in [#2202](https://github.com/mcdonc/klangk/issues/2202)).
- zfs delegation vs. a small setuid helper vs. running the lifecycle step as
  root — which fits klangkd's process model best.
- Seed build (#2200): driven by a devenv task (preferred per #2198) producing
  the seed dataset, vs. extract-from-image.
