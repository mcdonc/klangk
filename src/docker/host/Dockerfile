FROM python:3.13-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      coreutils \
      gzip \
      nginx \
      rsync \
      sqlite3 \
      supervisor \
      tar \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user matching devenv convention (uid 1000)
RUN groupadd -g 1000 klangk && \
    useradd -u 1000 -g klangk -m -d /home/klangk -s /bin/bash klangk

# ---- venv stage: copy pre-built venv and fix shebangs ----
FROM base AS venv
COPY --from=hostvenv . /home/klangk/venv/
# Rewrite hardcoded shebangs from the build host's python path
# to the container's python3 (handles uvicorn, klangk, etc.)
RUN find /home/klangk/venv/bin -type f -exec \
      sed -i '1s|^#!.*python[0-9.]*$|#!/usr/bin/env python3|' {} + 2>/dev/null; \
    true

# ---- final image ----
FROM base

COPY --from=venv --chown=klangk:klangk /home/klangk/venv /home/klangk/venv

# Backend source
COPY --chown=klangk:klangk src/backend/klangk_backend /home/klangk/src/backend/klangk_backend
COPY --chown=klangk:klangk src/backend/pyproject.toml /home/klangk/src/backend/pyproject.toml

# Flutter web build output
COPY --chown=klangk:klangk src/frontend/build/web /home/klangk/src/frontend/build/web

# Startup scripts and config
COPY --chown=klangk:klangk scripts/nginx.sh /home/klangk/bin/nginx
COPY --chown=klangk:klangk src/docker/host/entrypoint.sh /home/klangk/bin/entrypoint
COPY --chown=klangk:klangk src/docker/host/supervisord.conf /home/klangk/etc/supervisord.conf

# Version file (injected at build time via --build-arg)
ARG KLANGK_BUILD_VERSION=unknown
ARG KLANGK_BUILD_COMMIT=unknown
ARG KLANGK_BUILD_TIMESTAMP=unknown
RUN printf '{"version":"%s","commit":"%s","built_at":"%s"}\n' \
      "$KLANGK_BUILD_VERSION" "$KLANGK_BUILD_COMMIT" \
      "$KLANGK_BUILD_TIMESTAMP" \
      > /home/klangk/version.json && \
    chown klangk:klangk /home/klangk/version.json

# Data directory (volume mount point)
RUN mkdir -p /home/klangk/data && chown klangk:klangk /home/klangk/data

# Nginx needs writable temp dirs
RUN mkdir -p /tmp/nginx_client_body /tmp/nginx_proxy \
             /tmp/nginx_fastcgi /tmp/nginx_uwsgi /tmp/nginx_scgi && \
    chown -R klangk:klangk /tmp/nginx_*
# Nginx on Debian needs /var/log/nginx writable
RUN mkdir -p /var/log/nginx /var/lib/nginx && \
    chown -R klangk:klangk /var/log/nginx /var/lib/nginx

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
# Used by nginx.sh to write nginx.conf at runtime
ENV KLANGK_STATE_DIR=/tmp/klangk-state

USER klangk
WORKDIR /home/klangk

EXPOSE 8997 8995

ENTRYPOINT ["/home/klangk/bin/entrypoint"]
CMD ["start"]
