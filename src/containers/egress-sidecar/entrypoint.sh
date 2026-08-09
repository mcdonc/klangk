#!/bin/sh
# Egress sidecar startup (#2253): install the OUTPUT ruleset + the nat REDIRECT,
# then run the FQDN DNS proxy. Runs in the sidecar (NET_ADMIN) which shares the
# workspace's network namespace.
#
# Loop-avoidance: the nat REDIRECT targets the workspace's configured resolvers
# (read from this sidecar's /etc/resolv.conf — the workspace inherits them via
# --network container:<sidecar>); the proxy forwards to a DIFFERENT upstream
# ($KLANGKEGRESS_UPSTREAM), which the REDIRECT must not match. Any nameserver
# equal to the upstream is skipped (would loop).
set -eu

UPSTREAM="${KLANGKEGRESS_UPSTREAM:-8.8.8.8}"
IPT="${KLANGKEGRESS_IPTABLES:-iptables}"
LISTEN_PORT="${KLANGKEGRESS_LISTEN_PORT:-15353}"

# --- filter OUTPUT: default-deny + the paths the workspace + proxy need ---
$IPT -P OUTPUT DROP
# Loopback by *destination* (not -o lo): REDIRECT keeps the packet's original
# output interface, so -o lo misses the redirected :53 packet under a DROP policy.
$IPT -A OUTPUT -d 127.0.0.0/8 -j ACCEPT
$IPT -A OUTPUT -o lo -j ACCEPT
$IPT -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
$IPT -A OUTPUT -d "$UPSTREAM" -p udp --dport 53 -j ACCEPT
$IPT -A OUTPUT -d "$UPSTREAM" -p tcp --dport 53 -j ACCEPT

# --- static CIDR allow-specs (host specs are learned dynamically by the proxy).
# A "host:port" CIDR like 10.0.0.0/8:443 is stripped to its CIDR; per-domain
# port scoping is #2256.
IFS=','
for spec in ${KLANGKEGRESS_ALLOW:-}; do
  case "$spec" in
  */*)
    cidr=${spec%%:*}
    $IPT -A OUTPUT -d "$cidr" -j ACCEPT
    ;;
  esac
done
unset IFS

# --- backend reachability: allow the klangkd backend (LLM proxy + bridge)
# on host.containers.internal. The workspace must reach it to function, and
# host.containers.internal is a /etc/hosts entry (not a DNS lookup), so the
# FQDN proxy can never learn its IP — allow it statically, scoped to the
# backend port only (the klangkd backend is itself authenticated). #2254 B1.
case "${KLANGKEGRESS_BACKEND_PORT:-}" in
'' | *[!0-9]*) ;; # empty or non-numeric (e.g. "socket"): nothing to allow
*)
  gw=$(getent hosts host.containers.internal 2>/dev/null | awk '{print $1; exit}')
  [ -n "$gw" ] && $IPT -A OUTPUT -d "$gw" -p tcp --dport "$KLANGKEGRESS_BACKEND_PORT" -j ACCEPT
  ;;
esac

# --- nat OUTPUT: REDIRECT each configured resolver (:53) to the proxy ---
grep -E '^nameserver' /etc/resolv.conf | awk '{print $2}' | while read -r ns; do
  if [ "$ns" = "$UPSTREAM" ]; then
    # Loop-avoidance skip — but make it LOUD: a resolver equal to the
    # upstream is NOT redirected, so DNS to it bypasses the FQDN allow-list
    # in proxy.py. The wiring (klangk starting this sidecar with --dns set
    # to a resolver != upstream) must prevent this; until #2254 lands,
    # surface it rather than fail silently.
    echo "klangk-egress: resolver $ns == upstream $UPSTREAM; not redirecting" \
      "(would loop) — DNS to it is UNFILTERED. Ensure the workspace's" \
      "resolvers differ from the upstream (#2254)." >&2
    continue
  fi
  $IPT -t nat -A OUTPUT -d "$ns" -p udp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"
  $IPT -t nat -A OUTPUT -d "$ns" -p tcp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"
done

exec python3 /proxy.py
