# Egress sidecar image (#2253) — runs the FQDN DNS proxy in a NET_ADMIN
# container that shares a filtered workspace's network namespace.
#
# Build context is this directory (proxy.py + entrypoint.sh are COPY'd in).
FROM alpine:3.21
# Runtime: iptables + python3 + the C libs netfilterqueue links (the
# interactive-mode NFQUEUE consumer, #2242). Build deps are installed
# transiently to compile the netfilterqueue C extension, then removed.
RUN apk add --no-cache \
      iptables python3 py3-pip \
      libnetfilter_queue libmnl \
    && apk add --no-cache --virtual .build-deps \
      build-base python3-dev libnetfilter_queue-dev libmnl-dev \
    && pip install --no-cache-dir --break-system-packages \
      "dnspython>=2.7,<3" netfilterqueue \
    && apk del .build-deps
COPY proxy.py /proxy.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
