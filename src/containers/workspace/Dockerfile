ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.06.11-2603487
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
COPY setup_clankers.py /usr/local/bin/setup_clankers
RUN chmod +x /opt/klangk/entrypoint.sh /usr/local/bin/setup_clankers
COPY bash.bashrc /etc/bash.bashrc
COPY tmux.conf /etc/tmux.conf

ENTRYPOINT ["/opt/klangk/entrypoint.sh"]
