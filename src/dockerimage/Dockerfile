FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    procps \
    net-tools \
    lsof \
    telnet \
    iputils-ping \
    vim \
    emacs-nox \
    sqlite3 \
    unzip \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node/npm already present from base image
RUN npm install -g @mariozechner/pi-coding-agent

# Create bark user with host UID/GID (passed as build args)
ARG BARK_UID=1000
ARG BARK_GID=1000
RUN EXISTING_USER=$(getent passwd $BARK_UID | cut -d: -f1) \
    && [ -n "$EXISTING_USER" ] && userdel "$EXISTING_USER" || true \
    && EXISTING_GROUP=$(getent group $BARK_GID | cut -d: -f1) \
    && [ -n "$EXISTING_GROUP" ] && groupdel "$EXISTING_GROUP" 2>/dev/null || true \
    && groupadd -g $BARK_GID bark 2>/dev/null || true \
    && useradd -u $BARK_UID -g $BARK_GID -m -d /home/bark -s /bin/sh bark

RUN mkdir -p /workspace && chown bark:bark /workspace
WORKDIR /workspace

# Server-side tools per plugin (from plugin-tools named build context)
# Layout: /opt/bark/plugin-tools/<name>/<script>
COPY --from=plugin-tools / /opt/bark/plugin-tools/

# Pi extensions and entrypoint stored in /opt/bark (root-owned, read-only at runtime)
RUN mkdir -p /opt/bark/pi-agent/extensions
COPY --from=plugin-extensions / /opt/bark/pi-agent/extensions/
COPY builtin-extensions/ /opt/bark/pi-agent/extensions/
COPY system-prompt.md /opt/bark/system-prompt.md
COPY entrypoint.sh /opt/bark/entrypoint.sh
RUN chmod +x /opt/bark/entrypoint.sh

# Prevent bark user from escalating to root
RUN passwd -l root \
    && chmod u-s /usr/bin/su /usr/bin/chsh /usr/bin/chfn /usr/bin/newgrp \
    && rm -f /usr/bin/sudo 2>/dev/null || true

ENTRYPOINT ["/opt/bark/entrypoint.sh"]
