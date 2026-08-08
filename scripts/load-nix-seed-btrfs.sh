#!/usr/bin/env bash
# Load #2200's nix seed tree into a btrfs subvolume — the source that #2201's
# per-workspace snapshots (klangk.nix.Nix) are created from.
#
# Usage: load-nix-seed-btrfs.sh <seed-tree> <btrfs-parent>
#   <seed-tree>     output of `devenv shell -- build-nix-seed` (holds nix/ + nix.conf)
#   <btrfs-parent>  a directory on a btrfs filesystem mounted with
#                   `user_subvol_rm_allowed`, writable by the caller, e.g.
#                   /steam2/btrfs/klangk-nix. The seed subvolume lands at
#                   <btrfs-parent>/seed; set KLANGKD_NIX_BTRFS_SUBVOLUME to it.
#
# No root needed — btrfs lets a user create/snapshot subvolumes it can write to,
# and `user_subvol_rm_allowed` lets it delete them. (Contrast zfs: non-root mount
# is impossible on Linux, which would force a cap_sys_admin helper — #2210.)
#
# Reseeding: `btrfs subvolume delete <btrfs-parent>/seed` first. btrfs snapshots
# are independent CoW copies, so existing workspace snapshots keep their data
# after the seed is removed (unlike zfs clones, which depend on the snapshot).
set -euo pipefail

SEED_TREE="${1:?usage: load-nix-seed-btrfs.sh <seed-tree> <btrfs-parent>}"
PARENT="${2:?usage: load-nix-seed-btrfs.sh <seed-tree> <btrfs-parent>}"

require() { [ -e "$1" ] || {
  echo "ERROR: $1 not found (run 'devenv shell -- build-nix-seed' first)" >&2
  exit 1
}; }
require "$SEED_TREE/nix"
require "$SEED_TREE/nix.conf"

mkdir -p "$PARENT"
SEED="${PARENT}/seed"
if [ -e "$SEED" ]; then
  echo "ERROR: $SEED already exists — remove it first: btrfs subvolume delete '$SEED'" >&2
  exit 1
fi

echo "==> Creating seed subvolume $SEED"
btrfs subvolume create "$SEED"

echo "==> Populating from $SEED_TREE"
# store files are read-only (0444/0555); +w so cp can update on reseed paths.
chmod -R u+w "$SEED_TREE" 2>/dev/null || true
cp -a "$SEED_TREE/nix" "$SEED_TREE/nix.conf" "$SEED"/

echo "==> Done. Set in klangkd.yaml:"
echo "  nix_seed:"
echo "    type: btrfs-snapshot"
echo "    path: $SEED"
