#!/usr/bin/env bash
# Build the shared base /nix store seed (#2200) and extract it to a deployable
# host-side tree. The tree is consumed by #2201 — used as an overlayfs lowerdir
# or loaded into a zfs dataset for per-workspace cloning.
#
# Output layout at $OUT (default ./nix-base, overridable via $1 or
# $KLANGK_NIX_SEED_OUT):
#   $OUT/nix/        store + var/db + the base profile (nix, devenv, cachix)
#   $OUT/nix.conf    flakes/nix-command + pre-configured binary caches
#
# The build runs in a throwaway container (the nix-seed sandbox image); the
# container is a build sandbox only — its /nix is extracted, the image is not
# shipped or baked into any workspace (#2198: store deployed alongside klangk,
# not image-baked).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

IMAGE="klangk-nix-seed:latest"
OUT="${1:-${KLANGK_NIX_SEED_OUT:-$PWD/nix-base}}"
shift # remaining args (e.g. --no-cache) pass through to podman build

echo "==> Building nix-seed sandbox image"
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  -f src/containers/nix-seed/Dockerfile \
  -t "$IMAGE" \
  "$@" src/containers/nix-seed/

echo "==> Extracting /nix + nix.conf -> $OUT"
# Clear any prior output first (store files are read-only, so +w before rm).
mkdir -p "$OUT"
chmod -R u+w "$OUT" 2>/dev/null || true
rm -rf "${OUT:?}/nix" "${OUT:?}/nix.conf" "${OUT:?}/etc"
# Extract directly into $OUT (same filesystem -> no cross-fs copy/unlink of the
# read-only store files). Pulls /nix and /etc/nix/nix.conf from the sandbox fs.
cid="$("$PODMAN" create --entrypoint /bin/true "$IMAGE")"
"$PODMAN" export "$cid" | tar -x -C "$OUT" nix etc/nix/nix.conf
"$PODMAN" rm "$cid" >/dev/null
test -f "$OUT/etc/nix/nix.conf" || {
  echo "ERROR: /etc/nix/nix.conf missing from image" >&2
  exit 1
}
mv "$OUT/etc/nix/nix.conf" "$OUT/nix.conf"
rmdir -p "$OUT/etc/nix" "$OUT/etc" 2>/dev/null || true

# Restore read-write bits the seed tree needs to be usable on the host (nix
# store files are 0444/0555; the owner still needs +w to, e.g., load the tree
# into a dataset or move it).
chmod -R u+w "$OUT/nix" || true

echo "==> Verifying: nix runs against the extracted store"
# shellcheck disable=SC2016  # the inline sh is run by the container, not expanded here
"$PODMAN" run --rm --entrypoint /bin/sh \
  -v "$OUT/nix:/nix:ro" \
  -v "$OUT/nix.conf:/etc/nix/nix.conf:ro" \
  debian:trixie-slim -c '
    export PATH="/nix/nix-profile/bin:$PATH" NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    # The baked activation snippet sources this; fail the build early if a
    # future nix reshuffles the profile layout (#2202 auto-activation).
    test -f /nix/nix-profile/etc/profile.d/nix.sh
    nix --version && devenv --version
  '

echo "==> Done: $OUT"
echo "    $OUT/nix/      ($(du -sh "$OUT/nix" | cut -f1), $(find "$OUT/nix/store" -mindepth 1 -maxdepth 1 | wc -l) store entries)"
echo "    $OUT/nix.conf  (flakes/nix-command + binary caches)"
echo "Consume with #2201: overlay lowerdir, or load into a zfs dataset and snapshot."
