# Custom klangk-host image with plugins and optional CA certs.
#
# This cannot be built standalone — use ./build.sh which clones klangk,
# fetches plugins, rebuilds Flutter web and the workspace image, then
# builds this Dockerfile.
#
# Usage:
#   ./build.sh
#
FROM klangk-host:latest

# Add custom CA certificate (if provided)
COPY ssl/ /tmp/ssl/
USER root
RUN cp /tmp/ssl/*.pem /usr/local/share/ca-certificates/ 2>/dev/null; \
    cp /tmp/ssl/*.crt /usr/local/share/ca-certificates/ 2>/dev/null; \
    for f in /usr/local/share/ca-certificates/*.pem; do \
      [ -f "$f" ] && mv "$f" "${f%.pem}.crt"; \
    done; \
    update-ca-certificates; \
    rm -rf /tmp/ssl

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt    

USER klangk

# Mount point for run.sh
RUN mkdir -p /home/klangk/mount

# Custom OIDC login hook (PYTHONPATH includes /home/klangk/src/backend)
COPY --chown=klangk:klangk login_hook.py /home/klangk/src/backend/login_hook.py

# Replace Flutter web build (rebuilt with Dart plugins)
COPY --chown=klangk:klangk web /home/klangk/src/frontend/build/web

# Replace workspace tarball (rebuilt with plugin extensions/tools)
COPY --from=workspace-tar --chown=klangk:klangk workspace.tar /home/klangk/workspace.tar
