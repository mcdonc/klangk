# python:3.14-slim pinned by IMMUTABLE DIGEST (multi-arch manifest list)
# so the registry cannot serve different content for the tag (#2063).
# Bump: docker buildx imagetools inspect python:3.14-slim
# (read the `Digest:` line of the manifest list)
FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS base

# Rebased onto the 3.14 interpreter base for the toolchain upgrade
# (#2844); digest-only rebuilds also pick up security patches (e.g.
# CVE-2026-45447 openssl).
# The reverse proxy is Caddy (klangkd's default engine since #1559; nginx
# is gone — klangkd renders a Caddyfile and drives Caddy over its admin
# API). It is installed from the official GitHub release tarball, not from
# the Caddy apt repo on Cloudsmith: that repo publishes an InRelease
# signed with an expired subkey (531A6B20FA058A70 of the repo's key),
# which Debian trixie's apt — whose OpenPGP verifier sqv enforces key
# liveness — refuses, breaking every host-image build until Cloudsmith
# re-signs (#3502). No keyring change on this side can repair a repo
# signed with a dead subkey.
#
# Supply-chain posture (#2063): the URL is fixed by the pinned version and
# the tarball's SHA-256 is verified per architecture before install — an
# unverified fetch never reaches /usr/bin. The digests are the release's
# asset digests (GitHub's hash of the exact bytes served). Bump: pick the
# new version, fetch its amd64/arm64 digests from
# https://api.github.com/repos/caddyserver/caddy/releases/latest, and
# update all three ARGs together.
ARG CADDY_VERSION=2.11.4
ARG CADDY_SHA256_AMD64=527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9
ARG CADDY_SHA256_ARM64=52d42ae12b3462097e9868da6dfed3c9648ae12edd3b3638102312af84cb6904
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      catatonit \
      coreutils \
      curl \
      fuse-overlayfs \
      gzip \
      iproute2 \
      passt \
      podman \
      procps \
      rsync \
      sqlite3 \
      supervisor \
      tar \
      uidmap \
    && CADDY_ARCH="$(dpkg --print-architecture)" \
    && case "$CADDY_ARCH" in \
         amd64) CADDY_SHA256="$CADDY_SHA256_AMD64" ;; \
         arm64) CADDY_SHA256="$CADDY_SHA256_ARM64" ;; \
         *) echo "unsupported architecture $CADDY_ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
         "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_${CADDY_ARCH}.tar.gz" \
         -o /tmp/caddy.tar.gz \
    && echo "${CADDY_SHA256}  /tmp/caddy.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/caddy.tar.gz -C /tmp caddy \
    && install -m 0755 /tmp/caddy /usr/bin/caddy \
    && rm -f /tmp/caddy.tar.gz /tmp/caddy \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user matching devenv convention (uid 1000)
RUN groupadd -g 1000 klangk && \
    useradd -u 1000 -g klangk -m -d /home/klangk -s /bin/bash klangk

# ---- final image ----
FROM base

# Install the locally-built klangk wheel (#1603). pip pulls klangk's Python
# dependencies from PyPI into the base image's python — no borrowed devenv
# venv, no editable install, no PYTHONPATH hack. The wheel bundles the
# Flutter web build at klangk/frontend (#1600), so there's no separate
# frontend COPY either. (build-host-image.sh builds the wheel on the host
# via devenv/flutter; the final image depends on no devenv state.)
# Dependency versions come from uv.lock: build-host-image.sh exports the
# lock to host-image-constraints.txt and pip resolves inside those pins,
# so every build of the same commit installs the same dependency set —
# and Dockerfile.fips's crypto-relinker rebuilds exactly the locked
# cryptography from source.
COPY host-image-constraints.txt /tmp/constraints.txt
COPY src/klangk/dist/klangk-*.whl /tmp/
RUN pip install --constraint /tmp/constraints.txt /tmp/klangk-*.whl && \
    rm -f /tmp/klangk-*.whl /tmp/constraints.txt

# Startup scripts and config
COPY --chown=klangk:klangk src/containers/host/entrypoint.sh /home/klangk/bin/entrypoint
COPY --chown=klangk:klangk src/containers/host/supervisord.conf /home/klangk/etc/supervisord.conf
# klangkd runtime config — generated at build time (klangkd.yaml is gitignored
# by convention, so no tracked source). Passed via `klangkd --config` in
# supervisord.conf; no KLANGKD_* env vars in the image. frontend_dir is
# intentionally unset — the installed wheel serves the UI from klangk/frontend.
# listen=0.0.0.0 so the published browser port (EXPOSE 8997) is reachable via
# `docker -p`; the no-auth/loopback safety check (#1374) then requires auth to
# be configured (or KLANGKD_ALLOW_INSECURE_NO_AUTH) — an operator concern at run.
RUN mkdir -p /home/klangk/etc && printf '%s\n' \
      'port: "8997"' \
      'listen: "0.0.0.0"' \
      'egress_port: "8995"' \
      'data_dir: "/home/klangk/data"' \
      'customize_dir: "/home/klangk/custom"' \
      'image_name: "klangk-workspace"' \
      'version_file: "/home/klangk/version.json"' \
      'state_dir: "/tmp/klangk-state"' \
      > /home/klangk/etc/klangkd.yaml && \
    chown klangk:klangk /home/klangk/etc/klangkd.yaml

# Version file (generated by scripts/generate-version.sh, copied into build context)
COPY --chown=klangk:klangk version.json /home/klangk/version.json

# Data directory (volume mount point)
RUN mkdir -p /home/klangk/data && chown klangk:klangk /home/klangk/data

# Rootless podman: fuse-overlayfs for storage on overlayfs-backed filesystems
COPY --chown=klangk:klangk src/containers/host/storage.conf /home/klangk/.config/containers/storage.conf

# Embedded workspace image — loaded into podman at every host start (#3496)
COPY --from=workspace-image --chown=klangk:klangk workspace.tar /home/klangk/workspace.tar
# Embedded network sidecar image — loaded into podman at every host start (#2301, #3496)
COPY --from=network-sidecar-image --chown=klangk:klangk network-sidecar.tar /home/klangk/network-sidecar.tar


ENV HOME=/home/klangk
# klangk + its deps are in the base python's site-packages (pip install of
# the wheel above); klangkd is at /usr/local/bin. No PYTHONPATH, no venv/bin.
ENV PATH=/home/klangk/bin:$PATH
# klangkd's runtime config — the UDS bind, browser/egress ports, data/state/
# customize dirs, workspace image name, version file — lives in
# /home/klangk/etc/klangkd.yaml (passed via `klangkd --config` in
# supervisord.conf), not in KLANGKD_* env vars. The yaml is the single
# source of truth (#1603); env vars, if ever injected on the container,
# still override it.

USER klangk
WORKDIR /home/klangk

# Only the browser port is published; the egress port is internal
# (workspace containers reach it via host.containers.internal, not from
# outside the host). uvicorn is UDS-only.
EXPOSE 8997

ENTRYPOINT ["/home/klangk/bin/entrypoint"]
CMD ["start"]
