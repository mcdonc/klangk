# Base image is pinned by IMMUTABLE DIGEST (#2063): a mutable tag lets the
# registry serve different content for the same reference. The reference is
# the multi-arch manifest (amd64 + arm64); build-workspace-image.sh selects
# the platform via --platform. Bumped automatically by the auto-PR opened by
# .github/workflows/image-workspace-base.yml after each base-image build
# (the PR rewrites this line with the new repo@digest reference).
ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base@sha256:1f05ee5e8579507c710ab2b9e7c27536ce008cff12644f2329b9a97a9d94dc02
FROM $WORKSPACE_BASE_IMAGE

# Install Pi coding agent globally. The tarball is fetched directly from the
# npm registry and its SHA-512 verified against a pin before install (#2063):
# `npm install pkg@version` alone accepts whatever the registry serves for
# the version, with no pinned integrity. This layer is expensive (~30s) and
# rarely changes, so it goes first.
#
# Bump: set PI_AGENT_VERSION, then refresh PI_AGENT_SHA512 from the
# registry's own integrity record:
#   npm view @earendil-works/pi-coding-agent@<version> dist.integrity
# (base64 sha512 -> hex), or sha512sum of the fetched tarball.
ARG PI_AGENT_VERSION=0.83.0
ARG PI_AGENT_SHA512=b98845f85b19c68828497fc0c4171476263e66496e6f0697c80a04180d9e430b077321008546082a1fd62d77bf6bcfa4f1e4c29b16fe64dd99a3d9b342dcdb5b
RUN curl -fsSL -o /tmp/pi-agent.tgz \
      "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-${PI_AGENT_VERSION}.tgz" \
    && echo "${PI_AGENT_SHA512}  /tmp/pi-agent.tgz" | sha512sum -c - \
    && npm install -g /tmp/pi-agent.tgz \
    && rm -f /tmp/pi-agent.tgz \
    && mkdir -p /opt/klangk/pi-agent/extensions /opt/klangk/pi-agent/skills

# Copy full feature repos and symlink discoverable pieces into central dirs.
# Extensions get <feature>.ts, skills get <feature>-<skill>/, prompts get
# <feature>-<file>.md.  Features reference their own files directly via
# /opt/klangk/features/<name>/ (no tools symlinking).
COPY --from=features / /opt/klangk/features/
ENV PATH="/opt/klangk/bin:${PATH}"
RUN mkdir -p /opt/klangk/bin /opt/klangk/pi-agent/extensions \
             /opt/klangk/pi-agent/skills /opt/klangk/pi-agent/prompts \
    && echo 'PATH="/opt/klangk/bin:${PATH}"' >> /etc/environment \
    && for d in /opt/klangk/features/*/; do \
         [ -d "$d" ] || continue; \
         name=$(basename "$d"); \
         [ -f "$d/extension.ts" ] && ln -sf "$d/extension.ts" /opt/klangk/pi-agent/extensions/"$name.ts"; \
         if [ -d "$d/skills" ]; then \
           for s in "$d/skills"/*/; do \
             [ -d "$s" ] && ln -sf "$s" /opt/klangk/pi-agent/skills/"$name-$(basename "$s")"; \
           done; \
         fi; \
         if [ -d "$d/prompts" ]; then \
           for p in "$d/prompts"/*.md; do \
             [ -f "$p" ] && ln -sf "$p" /opt/klangk/pi-agent/prompts/"$name-$(basename "$p")"; \
           done; \
         fi; \
       done
COPY builtin-extensions/ /opt/klangk/pi-agent/extensions/

# Install uv (fast Python package manager). The GitHub release tarball is
# fetched directly and its SHA-256 verified per architecture before
# extraction (#2063) — no piped installer script: `curl | sh` executes
# whatever the URL serves, and the uv binary the installer then fetched was
# equally unverified.
#
# Bump: set UV_VERSION, then refresh both pins with sha256sum of each arch
# tarball (or the .sha256 sidecar files published alongside the release).
ARG UV_VERSION=0.11.23
ARG UV_SHA256_AMD64=e12c4cda2fe8c305510a78380a88f2c32a27e90cdcd123cefd2873388f0ebb5f
ARG UV_SHA256_ARM64=1873a77350f6621279ae1a0d2227f2bd8b67131598f14a7eb0ba2215d3da2c98
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) target=x86_64-unknown-linux-gnu; sha="$UV_SHA256_AMD64" ;; \
      arm64) target=aarch64-unknown-linux-gnu; sha="$UV_SHA256_ARM64" ;; \
      *) echo "unsupported arch for uv: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz"; \
    echo "${sha}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp "uv-${target}"; \
    install -m 0755 "/tmp/uv-${target}/uv" /usr/local/bin/uv; \
    install -m 0755 "/tmp/uv-${target}/uvx" /usr/local/bin/uvx; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${target}"; \
    uv --version && uvx --version

# Install process-compose supervisor (standalone Go binary, arch-aware).
# Extracts the single binary from the release tarball into /usr/local/bin
# so it is on PATH for all login and non-login shells. The tarball's
# SHA-256 is verified against a pin before extraction (#2063).
#
# Bump: set PROCESS_COMPOSE_VERSION, then refresh both pins with sha256sum
# of each arch tarball.
ARG PROCESS_COMPOSE_VERSION=1.120.0
ARG PROCESS_COMPOSE_SHA256_AMD64=3792e1ed9f383832eb2362154444e8564fbc8e7e8e7cff8754c68aea5eca086e
ARG PROCESS_COMPOSE_SHA256_ARM64=c5f4fcfc63e849279ac531bce2394a918fb28746339088a7d3d02bb5fb218a68
RUN set -eu; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
      amd64) sha="$PROCESS_COMPOSE_SHA256_AMD64" ;; \
      arm64) sha="$PROCESS_COMPOSE_SHA256_ARM64" ;; \
      *) echo "unsupported arch for process-compose: $ARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/process-compose.tar.gz \
      "https://github.com/F1bonacc1/process-compose/releases/download/v${PROCESS_COMPOSE_VERSION}/process-compose_linux_${ARCH}.tar.gz"; \
    echo "${sha}  /tmp/process-compose.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/process-compose.tar.gz -C /usr/local/bin process-compose; \
    chmod +x /usr/local/bin/process-compose; \
    process-compose version

# Entrypoint, agent context file, bash defaults (change frequently during dev)
COPY agent-context.md /opt/klangk/agent-context.md
COPY entrypoint.sh /opt/klangk/entrypoint.sh
COPY klangk-otel.sh /opt/klangk/bin/klangk-otel.sh
COPY klangk-setup-pi.py /opt/klangk/bin/klangk-setup-pi
COPY klangk-setup-home.sh /opt/klangk/bin/klangk-setup-home
COPY klangk-attach-browser.sh /opt/klangk/bin/klangk-attach-browser
COPY klangk-browser-id.sh /opt/klangk/bin/klangk-browser-id
COPY klangk-copy-to-clipboard.py /opt/klangk/bin/klangk-copy-to-clipboard
COPY klangk-configure-sudo.sh /opt/klangk/bin/klangk-configure-sudo
COPY klangk-hosted-url.sh /opt/klangk/bin/klangk-hosted-url
COPY klangk-set-workspace-token.sh /opt/klangk/bin/klangk-set-workspace-token
COPY klangk-workspace-token.sh /opt/klangk/bin/klangk-workspace-token
RUN chmod +x /opt/klangk/entrypoint.sh \
      /opt/klangk/bin/klangk-setup-pi \
      /opt/klangk/bin/klangk-setup-home \
      /opt/klangk/bin/klangk-attach-browser \
      /opt/klangk/bin/klangk-browser-id \
      /opt/klangk/bin/klangk-configure-sudo \
      /opt/klangk/bin/klangk-copy-to-clipboard \
      /opt/klangk/bin/klangk-hosted-url \
      /opt/klangk/bin/klangk-set-workspace-token \
      /opt/klangk/bin/klangk-workspace-token
# System-wide environment exports for ALL login shells (interactive AND
# non-interactive, e.g. `klangk exec`'s `bash -lc`). /etc/profile sources
# these unconditionally via run-parts. See issue #1093 for why env must live
# here rather than in /etc/bash.bashrc (which non-interactive shells skip).
# (The workspace health check is an exception: it runs a non-login `bash -c`
# and sources nothing — see docs/features/health-check.md.)
COPY profile.d/ /etc/profile.d/
COPY bash.bashrc /etc/bash.bashrc
COPY tmux.conf /etc/tmux.conf

# Run feature on-image-build hooks (alphabetical by feature name).
# These run as root at image build time for system-level config. No klangk
# feature installer spawns an interactive `bash -i` (hermes, the only one that
# did, is now a runtime sandbox -- #1109), so bash.bashrc's body never runs
# during image build and no build-time bailout is needed.
RUN for f in /opt/klangk/features/*/on-image-build.sh; do \
      [ -x "$f" ] || continue; \
      feature=$(basename "$(dirname "$f")"); \
      label=" $feature (on-image-build) "; \
      pad=$(( (60 - ${#label}) / 2 )); \
      line=$(printf '%*s' "$pad" '' | tr ' ' '-'); \
      printf '\033[33m%s%s%s\033[0m\n' "$line" "$label" "$line"; \
      "$f"; \
      printf '\033[33m%s\033[0m\n' "$(printf '%*s' 60 '' | tr ' ' '-')"; \
    done; true

# Fix non-root group ownership that causes crun fchownat errors with
# --userns=keep-id. crun cannot remap groups like utmp, shadow, adm, ssh
# in rootless user namespaces. The find handles most files; explicit
# chgrp catches files that xargs may skip due to batch error handling.
RUN chgrp root \
      /var/log/btmp /var/log/wtmp /var/log/lastlog \
      /var/log/apt/term.log \
      /etc/shadow /etc/shadow- /etc/gshadow /etc/gshadow- \
      /usr/bin/chage /usr/bin/expiry /usr/bin/ssh-agent \
      /usr/sbin/unix_chkpwd /var/mail \
    2>/dev/null; true

# Sudo is configured at runtime by the backend (klangk-configure-sudo, run via
# podman exec as the container root) based on KLANGKD_ALLOW_SUDO — no sudoers
# rule is baked into the image, and the entrypoint does not touch sudoers. The
# entrypoint itself runs as the klangk user (uid 1000) via the configured userns
# (default --userns=keep-id:uid=1000,gid=1000): there is no gosu drop and no
# root PID 1.

ENTRYPOINT ["/opt/klangk/entrypoint.sh"]
