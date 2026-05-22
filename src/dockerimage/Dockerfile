FROM ghcr.io/mcdonc/bark/bark-pi-base:latest

# Server-side tools per plugin (from plugin-tools named build context)
# Layout: /opt/bark/plugin-tools/<name>/<script>
COPY --from=plugin-tools / /opt/bark/plugin-tools/

# Pi extensions and entrypoint stored in /opt/bark (root-owned, read-only at runtime)
RUN mkdir -p /opt/bark/pi-agent/extensions
COPY --from=plugin-extensions / /opt/bark/pi-agent/extensions/
COPY builtin-extensions/ /opt/bark/pi-agent/extensions/
# Install npm dependencies for builtin extensions that need them
RUN for dir in /opt/bark/pi-agent/extensions/*/; do \
      [ -f "$dir/package.json" ] && (cd "$dir" && npm ci --omit=dev) || true; \
    done
COPY system-prompt.md /opt/bark/system-prompt.md
COPY entrypoint.sh /opt/bark/entrypoint.sh
RUN chmod +x /opt/bark/entrypoint.sh
COPY bash.bashrc /etc/bash.bashrc

ENTRYPOINT ["/opt/bark/entrypoint.sh"]
