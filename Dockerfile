# syntax=docker/dockerfile:1
#
# Self-contained "klangk daemon" image for the docker/podman compose path.
#
# This is ADDITIVE to the devenv flow — it does NOT replace
# src/containers/host/Dockerfile (built by `build-host-image` from a
# devenv-staged venv) nor the `devenv up` host-process flow. It packages the
# SAME runtime as the host image (uvicorn + nginx under supervisord, driven by
# the EXISTING entrypoint.sh / supervisord.conf / scripts/nginx.sh) and adds a
# rootless Podman engine so the one container can also spawn workspace
# containers — no Docker socket, no second service.
#
# Unlike src/containers/host/Dockerfile it is fully self-contained: it builds
# the Flutter web assets and the Python venv in-image, so `docker compose build`
# works with no devenv prestaging.
#
# This lives at the repo ROOT (named Dockerfile, the engine default) because
# podman-compose on Windows doesn't reliably forward a `dockerfile:` sub-path to
# the Linux engine (see docker-compose.yml). Its COPY paths are relative to the
# build context (the repo root). Build via:
#   docker compose build           # (docker == podman shim here)
#
# The rootless-Podman-in-a-container recipe is split across this image (the
# in-image half: non-root user + subuid/subgid + setuid newuidmap +
# fuse-overlayfs + *.conf) and docker-compose.yml (the host-side half: /dev/fuse,
# /dev/net/tun, security_opt, cap_add SYS_ADMIN). See docs/DOCKER-COMPOSE.md.

# ---------------------------------------------------------------------------
# Stage `web` — build the Flutter web assets (production flags only).
# ---------------------------------------------------------------------------
# Pinned to match devenv's nix toolchain (Flutter 3.41 / Dart 3.11 — see the
# flterm comment in scripts/flutterbuildweb.sh). The frontend pubspec floor is
# flutter ^3.27.0 / Dart ^3.6.0, which 3.41 satisfies. This tag is the one knob
# most likely to need a bump against the dev toolchain.
FROM ghcr.io/cirruslabs/flutter:3.41.9 AS web

# Replicate the repo layout (scripts/ and src/ as siblings) because
# stub_dart_plugins.sh resolves the frontend as "$(dirname $0)/../src/frontend".
WORKDIR /repo
COPY scripts/ /repo/scripts/
COPY src/frontend/ /repo/src/frontend/

# Normalize CRLF -> LF: a Windows checkout (core.autocrlf=true) gives these
# scripts CRLF endings, which break `set -euo pipefail\r` under Linux bash.
RUN sed -i 's/\r$//' scripts/*.sh

# Stub the klangk_plugins package so `flutter pub get` resolves without the full
# import_dart_plugins.py codegen (no plugins baked into the daemon image).
RUN bash scripts/stub_dart_plugins.sh

WORKDIR /repo/src/frontend
# Production build: drop the dev-only flags (--source-maps, --no-minify-*) and
# the inline_sources_in_map.py step that flutterbuildweb.sh uses for devtools.
RUN flutter --disable-analytics \
    && flutter pub get \
    && flutter build web --wasm --release --base-href=/ --no-web-resources-cdn \
    && rm -f build/web/flutter_service_worker.js

# Cache-bust flutter_bootstrap.js by content hash (index.html is served
# no-cache, so the ?v= query busts cached bootstrap/main.dart.* — mirrors the
# tail of scripts/flutterbuildweb.sh).
RUN set -eu; \
    for f in main.dart.wasm main.dart.js; do \
      if [ -f "build/web/$f" ]; then \
        HASH=$(sha256sum "build/web/$f" | cut -c1-12); break; \
      fi; \
    done; \
    sed -i "s|flutter_bootstrap.js|flutter_bootstrap.js?v=${HASH}|" build/web/index.html

# ---------------------------------------------------------------------------
# Stage `venv` — build the backend venv with uv (deps + project).
# ---------------------------------------------------------------------------
# python:3.13-slim is Debian trixie-based, matching the runtime stage, so the
# venv (lib/python3.13/site-packages) is ABI-compatible with the runtime's
# system python3.
FROM python:3.13-slim AS venv
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
COPY src/backend/ /build/src/backend/
# Build the venv at the same path the host image / supervisord PYTHONPATH expect.
RUN uv venv --python /usr/local/bin/python3 /home/klangk/venv \
    && uv pip install --python /home/klangk/venv/bin/python /build/src/backend

# ---------------------------------------------------------------------------
# Stage `runtime` — uvicorn + nginx (supervisord) + rootless Podman engine.
# ---------------------------------------------------------------------------
# debian:trixie-slim gives podman 5.x + passt (the proven rootless-nesting
# stack); its python3 is 3.13, matching the venv built above and the host
# image's expected lib/python3.13 path.
FROM debian:trixie-slim AS runtime

# Base runtime (mirrors src/containers/host/Dockerfile) + the rootless Podman
# engine and its plumbing:
#   nginx/supervisor       - the existing front + process manager (reused configs)
#   python3                - runs uvicorn (deps come from the copied venv)
#   bash/coreutils/...      - entrypoint + nginx.sh + workspace export/import
#   podman/crun/fuse-overlayfs/uidmap/passt/slirp4netns/catatonit
#                          - the in-process engine + rootless userns + netns +
#                            `podman --init` reaper (workspaces set Init=True)
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      catatonit \
      coreutils \
      crun \
      fuse-overlayfs \
      git \
      gzip \
      nginx \
      passt \
      podman \
      python3 \
      rsync \
      slirp4netns \
      sqlite3 \
      supervisor \
      tar \
      uidmap \
    && rm -rf /var/lib/apt/lists/*

# Non-root user matching the devenv/host-image convention (uid 1000), with
# subuid/subgid ranges so rootless podman can map IDs in its user namespace.
#
# The range starts at 1 (not the usual 100000) because this container is itself
# inside a rootless engine: it only has uids 0 + 1..65536 mapped (see the outer
# uid_map). A 100000-based range would point at uids that don't exist in the
# container, so the inner `newuidmap` fails with "Operation not permitted". A
# 1-based range fits within the container's mapping — the same pattern
# quay.io/podman/stable uses for rootless-podman-in-podman.
# Standard subuid/subgid range for rootless podman. This targets the PRODUCTION
# outer engine = Docker (a root daemon): the daemon container gets a full uid
# range and setuid newuidmap has real privilege, so 100000:65536 works (the
# proven podman-in-Docker recipe).
#
# NOTE: under a *rootless* outer engine (e.g. a local podman-machine), this
# container only has uids 0 + 1..65536 mapped, so a 100000-based range points at
# uids that don't exist and the inner newuidmap fails. Nested rootless-in-
# rootless additionally needs a range that fits 1..65536 AND skips uid 1000 — not
# pursued here since production is Docker.
RUN groupadd -g 1000 klangk \
    && useradd -u 1000 -g klangk -m -d /home/klangk -s /bin/bash klangk \
    && printf 'klangk:100000:65536\n' > /etc/subuid \
    && printf 'klangk:100000:65536\n' > /etc/subgid

# Debian ships newuidmap/newgidmap with file capabilities (xattrs) that often
# don't survive onto the overlay mount inside a container, dropping
# CAP_SETUID/SETGID -> "newuidmap: write to uid_map failed: Operation not
# permitted". The plain setuid bit (inode mode) is always preserved; this is
# what quay.io/podman/stable does for the same reason.
RUN chmod 4755 /usr/bin/newuidmap /usr/bin/newgidmap

# fuse-overlayfs uses /dev/fuse; allow non-root mounts.
RUN echo 'user_allow_other' >> /etc/fuse.conf

# Rootless-in-a-container engine/storage/registry config.
COPY src/containers/host/containers.conf /etc/containers/containers.conf
COPY src/containers/host/storage.conf /etc/containers/storage.conf
COPY src/containers/host/registries.conf /etc/containers/registries.conf

# Backend venv (deps + project) from the venv stage.
COPY --from=venv --chown=klangk:klangk /home/klangk/venv /home/klangk/venv

# Backend source — kept on PYTHONPATH (src first) so klangk_backend's __file__
# stays in the tree and main.py resolves frontend/build/web relative to it.
COPY --chown=klangk:klangk src/backend/klangk_backend /home/klangk/src/backend/klangk_backend
COPY --chown=klangk:klangk src/backend/pyproject.toml /home/klangk/src/backend/pyproject.toml

# Flutter web build output, at the path main.py expects (<src>/frontend/build/web).
COPY --from=web --chown=klangk:klangk /repo/src/frontend/build/web /home/klangk/src/frontend/build/web

# Existing startup scripts + config — REUSED VERBATIM from the devenv/host flow.
COPY --chown=klangk:klangk scripts/nginx.sh /home/klangk/bin/nginx
COPY --chown=klangk:klangk src/containers/host/entrypoint.sh /home/klangk/bin/entrypoint
COPY --chown=klangk:klangk src/containers/host/supervisord.conf /home/klangk/etc/supervisord.conf
# Normalize CRLF -> LF (Windows checkout) so the runtime shells/supervisord
# don't choke on \r in shebangs, `set -euo pipefail`, or config values.
RUN sed -i 's/\r$//' \
      /home/klangk/bin/nginx \
      /home/klangk/bin/entrypoint \
      /home/klangk/etc/supervisord.conf

# Version file (injected at build time via --build-arg).
ARG KLANGK_BUILD_VERSION=compose
ARG KLANGK_BUILD_COMMIT=unknown
ARG KLANGK_BUILD_TIMESTAMP=unknown
RUN printf '{"version":"%s","commit":"%s","built_at":"%s"}\n' \
      "$KLANGK_BUILD_VERSION" "$KLANGK_BUILD_COMMIT" \
      "$KLANGK_BUILD_TIMESTAMP" \
      > /home/klangk/version.json \
    && chown klangk:klangk /home/klangk/version.json

# Data dir + nginx temp/log dirs (writable as klangk) — same as the host image.
RUN mkdir -p /home/klangk/data && chown klangk:klangk /home/klangk/data
RUN mkdir -p /tmp/nginx_client_body /tmp/nginx_proxy \
             /tmp/nginx_fastcgi /tmp/nginx_uwsgi /tmp/nginx_scgi \
    && chown -R klangk:klangk /tmp/nginx_*
RUN mkdir -p /var/log/nginx /var/lib/nginx \
    && chown -R klangk:klangk /var/log/nginx /var/lib/nginx

# Pre-create podman's rootless store + a runtime dir owned by klangk *before*
# the compose volume mounts over the store: a named volume whose path is absent
# (or root-owned) in the image gets created root-owned, and rootless podman
# then can't write inside it.
RUN mkdir -p /home/klangk/.local/share/containers \
             /tmp/run-1000 \
    && chown -R klangk:klangk /home/klangk/.local /tmp/run-1000

ENV HOME=/home/klangk
ENV PYTHONPATH=/home/klangk/src/backend:/home/klangk/venv/lib/python3.13/site-packages
ENV PATH=/home/klangk/bin:/home/klangk/venv/bin:$PATH
ENV KLANGK_PORT=8997
ENV KLANGK_NGINX_PORT=8995
ENV KLANGK_DATA_DIR=/home/klangk/data
ENV KLANGK_PLUGINS_DIR=/home/klangk/plugins
ENV KLANGK_IMAGE_NAME=klangk
ENV KLANGK_INSTANCE_ID=default
ENV KLANGK_VERSION_FILE=/home/klangk/version.json
# Used by nginx.sh to write nginx.conf at runtime.
ENV KLANGK_STATE_DIR=/tmp/klangk-state
# Rootless podman needs a writable XDG_RUNTIME_DIR; there's no systemd to create
# /run/user/1000 in this container.
ENV XDG_RUNTIME_DIR=/tmp/run-1000
ENV KLANGK_PODMAN_BIN=podman

USER klangk
WORKDIR /home/klangk

# Sanity-check at build time that the engine binary initialises.
RUN podman --version

EXPOSE 8997 8995

ENTRYPOINT ["/home/klangk/bin/entrypoint"]
CMD ["start"]
