# Egress sidecar image (#2253) — runs the FQDN DNS proxy in a NET_ADMIN
# container that shares a filtered workspace's network namespace.
#
# Build context is this directory (entrypoint.sh is COPY'd in). The
# klangksidecar wheel arrives via a named context (``--build-context sidecar=``,
# staged by scripts/build-network-sidecar.sh from src/klangksidecar): the sidecar
# runs the *installed package* — its deps (dnspython + websockets) and the
# NFQUEUE consumer (netfilterqueue, via the [nfqueue] extra) come from the
# wheel's metadata — rather than a hand-copied proxy.py. proxy.py is a single
# module today, but the package will grow multifile, and a wheel install keeps
# working then (#2450). entrypoint.sh execs ``python3 -m proxy``.
# alpine:3.21 pinned by IMMUTABLE DIGEST (multi-arch manifest list, #2063).
# Bump: docker buildx imagetools inspect alpine:3.21
# (read the `Digest:` line of the manifest list)
FROM alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d
# Runtime: iptables + python3 + the C libs netfilterqueue links (the
# interactive-mode NFQUEUE consumer, #2242). Build deps are installed
# transiently to compile the netfilterqueue C extension (pulled in by the
# klangksidecar[nfqueue] extra), then removed.
COPY --from=sidecar / /tmp/sidecar-wheel/
RUN apk add --no-cache \
      iptables python3 py3-pip \
      libnetfilter_queue libmnl \
    && apk add --no-cache --virtual .build-deps \
      build-base python3-dev libnetfilter_queue-dev libmnl-dev \
    && WHL=$(echo /tmp/sidecar-wheel/*.whl) \
    && pip install --no-cache-dir --break-system-packages "${WHL}[nfqueue]" \
    && apk del .build-deps \
    && rm -rf /tmp/sidecar-wheel
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
