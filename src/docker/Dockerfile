FROM ghcr.io/mcdonc/bark/bark-base:latest

# Install npm-based Pi extensions globally via pi install.
# This layer is expensive (~30s) and rarely changes, so it goes first.
# Records packages in settings.json for Pi to discover at runtime.
RUN mkdir -p /opt/bark/pi-agent/extensions \
    && PI_CODING_AGENT_DIR=/opt/bark/pi-agent pi install npm:pi-otel-telemetry

# Server-side tools per plugin (from plugin-tools named build context)
# Layout: /opt/bark/plugin-tools/<name>/<script>
COPY --from=plugin-tools / /opt/bark/plugin-tools/

# Pi extensions from plugins and builtins (changes when plugins change)
COPY --from=plugin-extensions / /opt/bark/pi-agent/extensions/
COPY builtin-extensions/ /opt/bark/pi-agent/extensions/

# Entrypoint, system prompt, bash defaults (change frequently during dev)
COPY system-prompt.md /opt/bark/system-prompt.md
COPY entrypoint.sh /opt/bark/entrypoint.sh
COPY run-bark-pi.sh /usr/local/bin/bark-pi
RUN chmod +x /opt/bark/entrypoint.sh /usr/local/bin/bark-pi
COPY bash.bashrc /etc/bash.bashrc

ENTRYPOINT ["/opt/bark/entrypoint.sh"]
