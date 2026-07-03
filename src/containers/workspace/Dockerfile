ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.07.03-7a5caf9
FROM $WORKSPACE_BASE_IMAGE

# Install Pi coding agent and npm-based extensions globally.
# This layer is expensive (~30s) and rarely changes, so it goes first.
# Records packages in settings.json for Pi to discover at runtime.
RUN npm install -g @earendil-works/pi-coding-agent@0.79.9 \
    && mkdir -p /opt/klangk/pi-agent/extensions /opt/klangk/pi-agent/skills \
    && PI_CODING_AGENT_DIR=/opt/klangk/pi-agent pi install npm:@demigodmode/pi-web-agent@1.5.0

# Copy full plugin repos and symlink discoverable pieces into central dirs.
# Extensions get <plugin>.ts, skills get <plugin>-<skill>/, prompts get
# <plugin>-<file>.md.  Plugins reference their own files directly via
# /opt/klangk/plugins/<name>/ (no tools symlinking).
COPY --from=plugins / /opt/klangk/plugins/
ENV PATH="/opt/klangk/bin:${PATH}"
RUN mkdir -p /opt/klangk/bin /opt/klangk/pi-agent/extensions \
             /opt/klangk/pi-agent/skills /opt/klangk/pi-agent/prompts \
    && echo 'PATH="/opt/klangk/bin:${PATH}"' >> /etc/environment \
    && for d in /opt/klangk/plugins/*/; do \
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

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/0.11.23/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

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
COPY klangk-save-workspace-state.sh /opt/klangk/bin/klangk-save-workspace-state
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
      /opt/klangk/bin/klangk-save-workspace-state \
      /opt/klangk/bin/klangk-set-workspace-token \
      /opt/klangk/bin/klangk-workspace-token
# System-wide environment exports for ALL login shells (interactive AND
# non-interactive, e.g. `klangkc exec`'s `bash -lc`). /etc/profile sources
# these unconditionally via run-parts. See issue #1093 for why env must live
# here rather than in /etc/bash.bashrc (which non-interactive shells skip).
# (The workspace health check is an exception: it runs a non-login `bash -c`
# and sources nothing — see docs/features/health-check.md.)
COPY profile.d/ /etc/profile.d/
COPY bash.bashrc /etc/bash.bashrc
COPY tmux.conf /etc/tmux.conf

# Run plugin on-image-build hooks (alphabetical by plugin name).
# These run as root at image build time for system-level config. No klangk
# plugin installer spawns an interactive `bash -i` (hermes, the only one that
# did, is now a runtime sandbox -- #1109), so bash.bashrc's body never runs
# during image build and no build-time bailout is needed.
RUN for f in /opt/klangk/plugins/*/on-image-build.sh; do \
      [ -x "$f" ] || continue; \
      plugin=$(basename "$(dirname "$f")"); \
      label=" $plugin (on-image-build) "; \
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

# Sudo is configured at runtime by the entrypoint based on KLANGK_ALLOW_SUDO.
# No sudoers rule is baked into the image — the entrypoint runs as root
# (inside the user namespace) and writes or removes /etc/sudoers.d/klangk
# before dropping to the klangk user via gosu.

ENTRYPOINT ["/opt/klangk/entrypoint.sh"]
