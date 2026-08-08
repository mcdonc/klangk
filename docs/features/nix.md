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

## How workspaces consume it

The per-workspace mount (#2201) layers a writable, isolated view of the seed
on top of each nix-enabled workspace (overlay lowerdir, or — where a zfs pool
is available — a clone of a seed snapshot). nix/devenv are off `$PATH` by
default; the workspace image ships `/opt/klangk/bin/nix-activate.sh`, which a
user sources to put nix and nix-installed programs on `$PATH`.
