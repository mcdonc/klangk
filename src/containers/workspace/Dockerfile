ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.06.12-c965428
FROM $WORKSPACE_BASE_IMAGE

# Install npm-based Pi extensions globally via pi install.
# This layer is expensive (~30s) and rarely changes, so it goes first.
# Records packages in settings.json for Pi to discover at runtime.
RUN mkdir -p /opt/klangk/pi-agent/extensions \
    && PI_CODING_AGENT_DIR=/opt/klangk/pi-agent pi install npm:@demigodmode/pi-web-agent \
    && PI_CODING_AGENT_DIR=/opt/klangk/pi-agent pi install npm:pi-otel-telemetry \
    && PI_CODING_AGENT_DIR=/opt/klangk/pi-agent pi install npm:pi-mcp-extension

# Server-side tools per plugin (from plugin-tools named build context)
# Layout: /opt/klangk/plugin-tools/<name>/<script>
COPY --from=plugin-tools / /opt/klangk/plugin-tools/

# Pi extensions from plugins and builtins (changes when plugins change)
COPY --from=plugin-extensions / /opt/klangk/pi-agent/extensions/
COPY builtin-extensions/ /opt/klangk/pi-agent/extensions/

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
COPY otel.sh /opt/klangk/otel.sh
COPY setup_clankers.py /opt/klangk/bin/setup-clankers
COPY setup_user_pi.py /opt/klangk/bin/setup-user-pi
RUN chmod +x /opt/klangk/entrypoint.sh \
      /opt/klangk/bin/setup-clankers \
      /opt/klangk/bin/setup-user-pi
COPY bash.bashrc /etc/bash.bashrc
COPY tmux.conf /etc/tmux.conf

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

ENTRYPOINT ["/opt/klangk/entrypoint.sh"]
