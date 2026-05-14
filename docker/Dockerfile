FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    unzip \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node/npm already present from base image
RUN npm install -g @mariozechner/pi-coding-agent

# Dart SDK
RUN curl -fsSL https://storage.googleapis.com/dart-archive/channels/stable/release/latest/sdk/dartsdk-linux-x64-release.zip -o /tmp/dart.zip \
    && unzip -q /tmp/dart.zip -d /opt \
    && rm /tmp/dart.zip
ENV PATH="/opt/dart-sdk/bin:$PATH"

# Flutter SDK
RUN git config --global --add safe.directory /opt/flutter \
    && curl -fsSL https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_3.27.4-stable.tar.xz -o /tmp/flutter.tar.xz \
    && tar xf /tmp/flutter.tar.xz -C /opt \
    && rm /tmp/flutter.tar.xz \
    && /opt/flutter/bin/flutter precache --web
ENV PATH="/opt/flutter/bin:$PATH"

# Rust via rustup (install to /opt/rust so it's available to any user)
ENV RUSTUP_HOME=/opt/rust/rustup
ENV CARGO_HOME=/opt/rust/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/opt/rust/cargo/bin:$PATH"

RUN mkdir -p /workspace
WORKDIR /workspace

# Server-side tools
COPY tools/ /usr/local/bin/bark-tools/
RUN chmod +x /usr/local/bin/bark-tools/*.py

# Pi extensions and entrypoint stored in /opt/bark (root-owned, read-only at runtime)
RUN mkdir -p /opt/bark/pi-agent/extensions
COPY extensions/ /opt/bark/pi-agent/extensions/
COPY entrypoint.sh /opt/bark/entrypoint.sh
RUN chmod +x /opt/bark/entrypoint.sh

ENTRYPOINT ["/opt/bark/entrypoint.sh"]
