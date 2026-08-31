"""IPv4 packet parsing + forged-RST eager-deny for the NFQUEUE path (#2345, #2450).

parse_dest / parse_syn_tuple unpack an IPv4 L3/L4 payload; build_rst_packet
forges the RST the denied SYN_SENT socket accepts (ECONNREFUSED); send_rst
sends it via the IP_HDRINCL raw socket opened by check_rst_socket.
"""

from __future__ import annotations

import socket
import struct

from . import config


def rst_debug(msg: str) -> None:
    """Emit a forged-RST diagnostic line when ``KLANGKNETWORK_EGRESS_DEBUG_RST``
    is on (#2464).

    Centralized so the egress-smoketest diagnostic is one branch to cover, not
    one per call site in :func:`send_rst`. The smoketest enables the flag and
    captures the sidecar's podman log so a fast-refuse miss (a denied
    connection timing out instead of refusing fast) shows whether each RST
    fired.
    """
    if config.RST_DEBUG:
        print(msg, flush=True)


def ipv4_offsets(payload: bytes) -> tuple[int, int] | None:
    """(header_offset, ihl) for the IPv4 header, or None when the payload is
    not IPv4 (bare L3 or with a 14-byte Ethernet prefix) or too short."""
    off = 0
    if len(payload) > 14 and (payload[0] >> 4) != 4 and (payload[14] >> 4) == 4:
        off = 14  # Ethernet header
    if off + 20 > len(payload) or (payload[off] >> 4) != 4:
        return None
    ihl = (payload[off] & 0x0F) * 4
    if ihl < 20 or off + ihl > len(payload):
        return None
    return off, ihl


def parse_dest(payload: bytes) -> tuple[str, int]:
    """``(dst_ip, dst_port)`` from an IPv4 packet payload, or ``("", 0)``.

    The NFQUEUE payload may start at L3 (IP) or include a 14-byte Ethernet
    header; detect the IPv4 version nibble. Port is 0 for non-TCP/UDP. Pure
    so it can be unit-tested with synthetic bytes.
    """
    hdr = ipv4_offsets(payload)
    if hdr is None:
        return "", 0
    off, ihl = hdr
    proto = payload[off + 9]
    dst = ".".join(str(b) for b in payload[off + 16 : off + 20])
    port = 0
    if proto in (6, 17) and off + ihl + 4 <= len(
        payload
    ):  # TCP / UDP, L4 header present
        port = int.from_bytes(payload[off + ihl + 2 : off + ihl + 4], "big")
    return dst, port


def parse_syn_tuple(payload: bytes) -> tuple[str, int, str, int, int]:
    """``(src_ip, src_port, dst_ip, dst_port, seq)`` from an IPv4 TCP SYN, or an
    all-zero tuple if unparseable.

    The SYN's source end (IP:port) is the workspace's local end and becomes the
    forged RST's *destination*; its sequence number drives the RST's ack (#2345).
    Pure (mirrors :func:`parse_dest`'s L3/L4 offset logic) so it can be
    unit-tested with synthetic bytes.
    """
    hdr = ipv4_offsets(payload)
    if hdr is None:
        return "", 0, "", 0, 0
    off, ihl = hdr
    if payload[off + 9] != 6:  # TCP only
        return "", 0, "", 0, 0
    l4 = off + ihl
    if l4 + 12 > len(payload):  # sport + dport + seq
        return "", 0, "", 0, 0
    src_ip = ".".join(str(b) for b in payload[off + 12 : off + 16])
    dst_ip = ".".join(str(b) for b in payload[off + 16 : off + 20])
    src_port = int.from_bytes(payload[l4 : l4 + 2], "big")
    dst_port = int.from_bytes(payload[l4 + 2 : l4 + 4], "big")
    seq = int.from_bytes(payload[l4 + 4 : l4 + 8], "big")
    return src_ip, src_port, dst_ip, dst_port, seq


def ones_checksum(data: bytes) -> int:
    """RFC 1071 ones-complement checksum over ``data`` (pad odd length)."""
    if len(data) % 2:
        data = data + b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += int.from_bytes(data[i : i + 2], "big")
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_rst_packet(
    src_ip: str, src_port: int, dst_ip: str, dst_port: int, seq: int
) -> bytes:
    """A 40-byte IPv4 TCP RST+ACK packet that forges the eager deny (#2345).

    ``src`` is the denied host (the workspace's remote end) so the RST matches
    the SYN_SENT socket's 4-tuple; ``dst`` is the workspace's local end.
    Replicates the kernel's SYN-to-closed-port RST: RST+ACK with ``seq=0`` and
    ``ack=seq+1`` (a SYN consumes one sequence number), so the workspace's
    SYN_SENT socket accepts it and ``connect()`` returns ECONNREFUSED. The TCP
    checksum is computed over the IPv4 pseudo-header + the TCP header; the IP
    checksum is left 0 for the kernel to fill (IP_HDRINCL). Pure so it can be
    unit-tested with synthetic bytes.
    """
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,  # version 4, IHL 5 (20-byte header)
        0,  # tos
        40,  # total length (20 IP + 20 TCP)
        0,  # id (kernel fills under IP_HDRINCL)
        0,  # flags + fragment offset
        64,  # ttl
        6,  # proto TCP
        0,  # IP checksum (kernel fills under IP_HDRINCL)
        socket.inet_aton(src_ip),
        socket.inet_aton(dst_ip),
    )
    doff_flags = (5 << 12) | 0x14  # data offset 5 (20 B) | RST(0x04) + ACK(0x10)
    tcp_no_cksum = struct.pack(
        "!HHIIHHHH",
        src_port,
        dst_port,
        0,  # seq
        (seq + 1) & 0xFFFFFFFF,  # ack
        doff_flags,
        0,  # window
        0,  # checksum placeholder
        0,  # urgent pointer
    )
    pseudo = (
        socket.inet_aton(src_ip)
        + socket.inet_aton(dst_ip)
        + struct.pack("!BBH", 0, 6, len(tcp_no_cksum))
    )
    cksum = ones_checksum(pseudo + tcp_no_cksum)
    tcp_hdr = tcp_no_cksum[:16] + struct.pack("!H", cksum) + tcp_no_cksum[18:]
    return ip_hdr + tcp_hdr


# The raw socket used to forge the eager-deny RST (#2345). Opened lazily at
# startup (:func:`check_rst_socket`) with IP_HDRINCL so the denied host can be
# spoofed as the RST source (the workspace's SYN_SENT socket matches the remote
# tuple). ``None`` until then (also when consent is off or NET_RAW is absent);
# :func:`send_rst` then no-ops and the REJECT rule is the only fail-fast path.
RST_SOCK: socket.socket | None = None


def check_rst_socket() -> None:
    """Open the raw socket used to forge the eager-deny RST (#2345).

    Needs CAP_NET_RAW (the sidecar gets it). Best-effort: if the socket can't
    be opened, the REJECT rule remains the only fail-fast path (no behavior
    change). Logged at startup like :func:`check_mark`, and set non-blocking so
    :func:`send_rst` is safe to call inline on the loop thread.
    """
    global RST_SOCK
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
        s.setblocking(False)
        RST_SOCK = s
    except OSError as exc:
        print(
            f"dns-proxy: cannot open RST socket ({exc}); eager-deny falls "
            "back to REJECT only (#2345)",
            flush=True,
        )


def send_rst(payload: bytes) -> None:
    """Forge a RST to the workspace's SYN_SENT socket so ``connect()`` gets
    ECONNREFUSED at once, independent of conntrack/retransmit timing (#2345).

    Parses the held SYN's source end + seq, builds a RST sourced from the
    denied host, and sends it via the IP_HDRINCL raw socket (the dst is the
    workspace's own address, so the kernel routes it to lo and loops it back to
    the local TCP stack). No-op if the socket isn't open (consent disabled / no
    NET_RAW) -- then the REJECT rule is the only fail-fast path. Non-blocking
    (the socket is non-blocking; a 40-byte sendto won't block), so it is safe
    inline on the loop thread. Swallows errors: a transient send failure just
    leaves the REJECT backstop.
    """
    sock = RST_SOCK
    if sock is None:
        rst_debug("rst-forge: no raw socket (NET_RAW?) -- REJECT-only fast-refuse")
        return
    src_ip, src_port, dst_ip, dst_port, seq = parse_syn_tuple(payload)
    if not src_ip or not dst_port:
        rst_debug(f"rst-forge: unparseable tuple (src={src_ip} dst_port={dst_port})")
        return
    try:
        sock.sendto(
            # RST source = the denied host (dst of the SYN); dest = the
            # workspace's local end (src of the SYN). sendto routes to the
            # workspace's local address so it loops back to local INPUT.
            build_rst_packet(dst_ip, dst_port, src_ip, src_port, seq),
            (src_ip, 0),
        )
        rst_debug(
            f"rst-forge: sent {dst_ip}:{dst_port} -> {src_ip}:{src_port} "
            f"ack={(seq + 1) & 0xFFFFFFFF}"
        )
    except OSError as exc:
        rst_debug(f"rst-forge: sendto failed: {exc!r}")
