# Egress sidecar image (#2253) — runs the FQDN DNS proxy in a NET_ADMIN
# container that shares a filtered workspace's network namespace.
#
# Build context is this directory (proxy.py + entrypoint.sh are COPY'd in).
FROM alpine:latest
RUN apk add --no-cache iptables python3
COPY proxy.py /proxy.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
