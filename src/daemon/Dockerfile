# syntax=docker/dockerfile:1
#
# Multi-stage build for the **klangk daemon** (FastAPI backend + rootless
# Podman engine).  This is an alternative to the devenv/nix flow for shipping;
# devenv.nix stays the source of truth for local dev.  See DOCKER.md §6.
#
# Build from the repo ROOT so both src/frontend and src/backend are in context:
#   docker build -f src/daemon/Dockerfile -t klangk-daemon .
#
# The nginx front is a separate off-the-shelf service (see docker-compose.yml +
# src/daemon/nginx.conf.template); this image is backend + engine only.

# ---------------------------------------------------------------------------
# Stage `web` — build the Flutter web assets (production flags only).
# ---------------------------------------------------------------------------
# NOTE: pinned to match devenv's nix toolchain (Flutter 3.41 / Dart 3.11 — see
# the flterm comment in scripts/flutterbuildweb.sh).  The frontend pubspec floor
# is only `flutter: ^3.27.0` / Dart `^3.6.0`, which 3.41 satisfies.  **This tag
# is the one knob to confirm against your dev toolchain** — a mismatch here is
# the most likely build break.
FROM ghcr.io/cirruslabs/flutter:3.41.9 AS web

# Replicate the repo layout (scripts/ and src/ as siblings under /repo) because
# stub_dart_plugins.sh resolves the frontend as `$(dirname $0)/../src/frontend`.
WORKDIR /repo
# Scripts first (cache-friendly: rarely change relative to app code).
COPY scripts/ /repo/scripts/
COPY src/frontend/ /repo/src/frontend/

# Stub the klangk_plugins package so `flutter pub get` resolves without the
# full import_dart_plugins.py codegen (no plugins baked into the daemon image).
RUN bash scripts/stub_dart_plugins.sh

WORKDIR /repo/src/frontend
# Production build: drop the dev-only flags (--source-maps, --no-minify-*,
# --no-strip-wasm) and the inline_sources_in_map.py step that flutterbuildweb.sh
# uses — those exist for devtools source resolution and bloat the assets.
RUN flutter --disable-analytics \
    && flutter pub get \
    && flutter build web --wasm --release --base-href=/ --no-web-resources-cdn \
    && rm -f build/web/flutter_service_worker.js

# Cache-bust flutter_bootstrap.js by content hash (index.html is served
# no-cache, so the ?v= query busts cached bootstrap/main.dart.* — mirrors
# the tail of scripts/flutterbuildweb.sh).
RUN set -eu; \
    for f in main.dart.wasm main.dart.js; do \
      if [ -f "build/web/$f" ]; then \
        HASH=$(sha256sum "build/web/$f" | cut -c1-12); break; \
      fi; \
    done; \
    sed -i "s|flutter_bootstrap.js|flutter_bootstrap.js?v=${HASH}|" build/web/index.html

# ---------------------------------------------------------------------------
# Stage `runtime` — backend (uvicorn) + rootless Podman-in-Docker.
# ---------------------------------------------------------------------------
# Based on debian:trixie-slim to match the proven rootless-podman recipe in
# /home/bryan/src/podman_test (podman 5.x + passt).  trixie's python3 is 3.13,
# which satisfies the backend's `requires-python >=3.12` and matches the dev
# venv.  The rootless plumbing below (non-root user + subuid/subgid + setuid
# newuidmap + fuse.conf) is the in-image half of DOCKER.md §7; the matching
# host-side flags (devices, security_opt, cap_add) live in docker-compose.yml.
FROM debian:trixie-slim AS runtime

ARG USER=klangk
ARG UID=1000
ARG GID=1000
ENV DEBIAN_FRONTEND=noninteractive

# Engine + rootless plumbing + backend runtime deps:
#  - podman/crun/fuse-overlayfs: the in-process engine; nested rootless needs
#    fuse-overlayfs (overlay2/vfs don't work cleanly nested, needs /dev/fuse).
#  - uidmap: newuidmap/newgidmap setuid helpers for the rootless userns.
#  - passt/slirp4netns: userspace networking for rootless containers (/dev/net/tun).
#  - catatonit: tiny init for `podman run --init` (the pi containers use Init=True)
#    and as this container's PID 1 reaper.
#  - git/rsync: CLI shell-outs + workspace export/import.
#  - python3: runs the backend (uv builds a venv against it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        catatonit \
        curl \
        crun \
        fuse-overlayfs \
        git \
        passt \
        podman \
        python3 \
        rsync \
        slirp4netns \
        uidmap \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user that runs uvicorn *and* rootless podman, with subuid/subgid
# ranges so podman can map a range of IDs inside its user namespace.
RUN groupadd -g ${GID} ${USER} \
    && useradd -u ${UID} -g ${GID} -m -s /bin/bash ${USER} \
    && printf '%s:100000:65536\n' "${USER}" > /etc/subuid \
    && printf '%s:100000:65536\n' "${USER}" > /etc/subgid

# Debian ships newuidmap/newgidmap with *file capabilities* (xattrs) that often
# don't survive onto the overlay mount inside a container, dropping
# CAP_SETUID/SETGID -> "newuidmap: write to uid_map failed: Operation not
# permitted".  The plain setuid bit (inode mode) is always preserved; this is
# what quay.io/podman/stable does for the same reason.
RUN chmod 4755 /usr/bin/newuidmap /usr/bin/newgidmap

# fuse-overlayfs uses /dev/fuse; allow non-root mounts.
RUN echo 'user_allow_other' >> /etc/fuse.conf

# Engine/storage/registry config tuned for rootless-in-a-container (see files).
COPY src/daemon/containers.conf /etc/containers/containers.conf
COPY src/daemon/storage.conf /etc/containers/storage.conf
COPY src/daemon/registries.conf /etc/containers/registries.conf

# Pinned uv, used to build a venv and install the backend.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Lay the source out so main.py's web-assets lookup resolves unchanged:
# main.py does Path(__file__).parent.parent.parent / "frontend/build/web",
# i.e. <src>/frontend/build/web.  Keep src/backend + src/frontend under /app/src
# and install the backend EDITABLE so __file__ stays in the source tree.
WORKDIR /app
COPY src/backend/ /app/src/backend/
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
RUN uv venv --python /usr/bin/python3 "$VIRTUAL_ENV" \
    && uv pip install --no-cache -e /app/src/backend

# Web assets from the build stage, at the path main.py expects.
COPY --from=web /repo/src/frontend/build/web /app/src/frontend/build/web

# Pre-create state dirs owned by the unprivileged user *before* the volumes
# mount over them: a named volume whose path is absent (or root-owned) in the
# image gets created root-owned, and rootless podman / the backend then can't
# write inside it.  Covers both the app data dir and podman's rootless store.
RUN mkdir -p /var/lib/klangk/data \
             /home/${USER}/.local/share/containers \
    && chown -R ${UID}:${GID} /var/lib/klangk /home/${USER}/.local

# Backend defaults (overridable via compose environment).
#  - KLANGK_HOST_GATEWAY: how a pi container reaches the nginx front. Default
#    host.containers.internal is podman's native alias; this is the §8 knob to
#    confirm during the smoke test (the backend also `--add-host`s it).
ENV KLANGK_DATA_DIR=/var/lib/klangk/data \
    KLANGK_PORT=8997 \
    KLANGK_HOST_GATEWAY=host.containers.internal \
    KLANGK_PODMAN_BIN=podman \
    HOME=/home/${USER}

USER ${USER}

# Sanity-check at build time that the engine binary initialises.
RUN podman --version

EXPOSE 8997
COPY --chmod=755 src/daemon/start.sh /usr/local/bin/start.sh

ENTRYPOINT ["/usr/bin/catatonit", "--"]
CMD ["/usr/local/bin/start.sh"]
