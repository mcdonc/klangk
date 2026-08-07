#!/usr/bin/env bash
# Load #2200's nix seed tree into a zfs dataset and snapshot it at @base — the
# immutable base that #2201's per-workspace clones (klangk.nix.Nix) are created
# from.
#
# Usage: load-nix-seed-zfs.sh <seed-tree> <zfs-parent>
#   <seed-tree>   output of `devenv run build-nix-seed` (holds nix/ and nix.conf)
#   <zfs-parent>  zfs dataset parent, e.g. "d/klangk-nix". The seed lands at
#                 <zfs-parent>/seed and is snapshotted at <zfs-parent>/seed@base.
#
# This is an OPERATOR setup step (run once, or to reseed). It needs zfs
# create/mount/snapshot privilege — either run as root, or delegate once:
#
#   sudo zfs allow <klangkd-user> create,clone,destroy,mount,snapshot <pool>
#
# (the same delegation lets the klangkd runtime clone/destroy per-workspace
# datasets without full root).
#
# Reseeding destroys the seed snapshot that existing workspace clones depend on;
# the script refuses to clobber an existing seed — destroy it explicitly first.
set -euo pipefail

SEED_TREE="${1:?usage: load-nix-seed-zfs.sh <seed-tree> <zfs-parent>}"
PARENT="${2:?usage: load-nix-seed-zfs.sh <seed-tree> <zfs-parent>}"

require() { [ -e "$1" ] || {
  echo "ERROR: $1 not found (run 'devenv run build-nix-seed' first)" >&2
  exit 1
}; }
require "$SEED_TREE/nix"
require "$SEED_TREE/nix.conf"

SEED="${PARENT}/seed"
if zfs list "$SEED" >/dev/null 2>&1; then
  echo "ERROR: $SEED already exists — workspace clones may depend on its @base." >&2
  echo "       To reseed: zfs destroy -r $SEED  (then rerun this script)." >&2
  exit 1
fi

# Create the parent (if needed) and the seed dataset.
zfs list "$PARENT" >/dev/null 2>&1 || zfs create "$PARENT"
zfs create "$SEED"
MNT="$(zfs list -H -o mountpoint "$SEED")"

echo "==> Populating $SEED ($MNT) from $SEED_TREE"
# store files are read-only (0444/0555); +w so cp can update on reseed paths.
chmod -R u+w "$SEED_TREE" 2>/dev/null || true
cp -a "$SEED_TREE/nix" "$SEED_TREE/nix.conf" "$MNT"/

echo "==> Snapshotting $SEED@base (immutable base for per-workspace clones)"
zfs snapshot "$SEED@base"

echo "==> Done. Enable per-workspace nix with:"
echo "    KLANGKD_NIX_ENABLED=true KLANGKD_NIX_ZFS_DATASET=$PARENT"
