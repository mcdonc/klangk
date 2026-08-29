# Build sandbox for the shared base /nix store seed (#2200).
#
# This image ONLY exists to produce a self-consistent /nix tree (store +
# var/db + a base profile with nix and devenv) and an /etc/nix/nix.conf
# (flakes/nix-command + the devenv binary cache pre-configured).
# klangk-build-nix-seed builds it and extracts both into a deployable
# host-side tree that #2201 consumes — as an overlayfs lowerdir, or loaded
# into a zfs dataset for cloning.
#
# It is a BUILD SANDBOX, not a workspace image: nothing here is baked into a
# running workspace. The seed store is content-addressed, so the base image
# choice (debian:trixie-slim) only affects the build environment, not the
# resulting /nix content. Still pinned by digest (same manifest as
# src/containers/workspace/Dockerfile.base, #2063) so the sandbox itself
# cannot silently change under a mutable tag.

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

ARG NIX_INSTALL_URL=https://nixos.org/nix/install
ARG DEVENV_FLAKE=github:cachix/devenv/latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Single-user nix needs a non-root user that owns /nix (uid 1000 matches the
# workspace's klangk user, so ownership lines up when the tree is consumed).
RUN groupadd -g 1000 klangk \
    && useradd -u 1000 -g 1000 -m -d /home -s /bin/bash klangk \
    && mkdir -p /nix \
    && chown klangk:klangk /nix /home

# Enable flakes + nix-command system-wide (needed by the profile installs
# below). Binary-cache config is added post-install via `cachix use` so the
# devenv.cachix.org public key is always current (hardcoding it is fragile).
RUN mkdir -p /etc/nix \
    && printf 'experimental-features = nix-command flakes\n' > /etc/nix/nix.conf

USER klangk
# Single-user install (--no-daemon): the klangk user owns /nix directly — no
# nix-daemon. Produces /nix/store + /nix/var/nix/db/db.sqlite, self-consistent.
RUN curl -fsSL "$NIX_INSTALL_URL" | sh -s -- --no-daemon

# Install devenv + cachix into the base profile. --accept-flake-config pulls
# devenv's deps from devenv.cachix.org (substituter declared by the devenv
# flake itself), avoiding a slow from-source build of its Rust closure.
ENV USER=klangk
RUN export PATH="/home/.local/state/nix/profiles/profile/bin:$PATH" \
    && nix profile install nixpkgs#cachix \
    && nix profile install --accept-flake-config "$DEVENV_FLAKE"

# Pre-configure the devenv binary cache for consumers. `cachix use` writes to
# /etc/nix/nix.conf only when it believes it is root; the ENV USER=klangk above
# makes it think otherwise, so override USER for this one command.
USER root
RUN export PATH="/home/.local/state/nix/profiles/profile/bin:$PATH" \
    && USER=root cachix use devenv

# Expose the base profile at /nix/nix-profile, pointing at its STORE path (not
# the /home symlink) so the extracted /nix tree is self-contained — the
# installer's /home/.nix-profile lives under /home, which isn't part of the
# seed tree consumers mount.
RUN ln -s "$(readlink -f /home/.local/state/nix/profiles/profile)" /nix/nix-profile
