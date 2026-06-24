ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.06.24-fc6981c
FROM $WORKSPACE_BASE_IMAGE

# Install Pi coding agent and npm-based extensions globally.
# This layer is expensive (~30s) and rarely changes, so it goes first.
# Records packages in settings.json for Pi to discover at runtime.
RUN npm install -g @earendil-works/pi-coding-agent@0.79.9 \
    && mkdir -p /opt/klangk/pi-agent/extensions /opt/klangk/pi-agent/skills \
    && PI_CODING_AGENT_DIR=/opt/klangk/pi-agent pi install npm:@demigodmode/pi-web-agent@1.5.0

# Plugin tools: flat directory, all on PATH via /opt/klangk/bin/
ENV PATH="/opt/klangk/bin:${PATH}"
COPY --from=plugin-tools / /opt/klangk/bin/
RUN echo 'PATH="/opt/klangk/bin:${PATH}"' >> /etc/environment

# Plugin lifecycle hooks: /opt/klangk/hooks/<plugin>/{on-image-build,on-entrypoint,on-shell-init}.sh
COPY --from=plugin-hooks / /opt/klangk/hooks/

# Pi extensions from plugins and builtins (changes when plugins change)
COPY --from=plugin-extensions / /opt/klangk/pi-agent/extensions/
COPY builtin-extensions/ /opt/klangk/pi-agent/extensions/

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/0.11.23/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

# Herdr: terminal-based agent runtime (persistent sessions, pane API)
ARG HERDR_VERSION=0.6.6
ARG TARGETARCH
RUN HERDR_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "aarch64" || echo "x86_64") \
    && curl -fsSL -o /usr/local/bin/herdr \
    "https://github.com/ogulcancelik/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${HERDR_ARCH}" \
    && chmod +x /usr/local/bin/herdr

# Entrypoint, system prompt, bash defaults (change frequently during dev)
COPY system-prompt.md /opt/klangk/system-prompt.md
COPY entrypoint.sh /opt/klangk/entrypoint.sh
COPY klangk-otel.sh /opt/klangk/bin/klangk-otel.sh
COPY klangk-setup-clankers.py /opt/klangk/bin/klangk-setup-clankers
COPY klangk-setup-home.sh /opt/klangk/bin/klangk-setup-home
COPY klangk-attach-browser.sh /opt/klangk/bin/klangk-attach-browser
COPY klangk-browser-id.sh /opt/klangk/bin/klangk-browser-id
COPY klangk-copy-to-clipboard.py /opt/klangk/bin/klangk-copy-to-clipboard
COPY klangk-configure-sudo.sh /opt/klangk/bin/klangk-configure-sudo
COPY klangk-save-workspace-state.sh /opt/klangk/bin/klangk-save-workspace-state
COPY klangk-set-workspace-token.sh /opt/klangk/bin/klangk-set-workspace-token
COPY klangk-workspace-token.sh /opt/klangk/bin/klangk-workspace-token
RUN chmod +x /opt/klangk/entrypoint.sh \
      /opt/klangk/bin/klangk-setup-clankers \
      /opt/klangk/bin/klangk-setup-home \
      /opt/klangk/bin/klangk-attach-browser \
      /opt/klangk/bin/klangk-browser-id \
      /opt/klangk/bin/klangk-configure-sudo \
      /opt/klangk/bin/klangk-copy-to-clipboard \
      /opt/klangk/bin/klangk-save-workspace-state \
      /opt/klangk/bin/klangk-set-workspace-token \
      /opt/klangk/bin/klangk-workspace-token
COPY bash.bashrc /etc/bash.bashrc
COPY tmux.conf /etc/tmux.conf

# Run plugin on-image-build hooks (alphabetical by plugin name).
# These run as root at image build time for system-level config.
# /tmp/.klangk-image-build tells bash.bashrc to bail out early so that any
# `bash -i` spawned by plugin installers doesn't run user-session setup.
# Removed after hooks finish so it doesn't persist into the final image.
RUN touch /tmp/.klangk-image-build \
    && for f in /opt/klangk/hooks/*/on-image-build.sh; do \
      [ -x "$f" ] && "$f"; \
    done; rm -f /tmp/.klangk-image-build; true

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

# Sudo is configured at runtime by the entrypoint based on KLANGK_ALLOW_SUDO.
# No sudoers rule is baked into the image — the entrypoint runs as root
# (inside the user namespace) and writes or removes /etc/sudoers.d/klangk
# before dropping to the klangk user via gosu.

ENTRYPOINT ["/opt/klangk/entrypoint.sh"]
