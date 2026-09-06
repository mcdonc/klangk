"""DPoP proof-of-possession helpers (RFC 9449) for session-token binding.

Why DPoP (#3218): the audit findings V-222575/V-222576 want the session
credential out of JavaScript reach — but browser storage a script can
read is storage a script can steal, whatever it is named. Binding the
JWT to a key the browser refuses to export flips the property: the web
client generates an ECDSA P-256 keypair via WebCrypto and re-imports
the private half with ``extractable: false``, persisting that handle in
IndexedDB (structured clone preserves non-extractability). The JWT
carries the public key's RFC 7638 thumbprint in ``cnf.jkt``; every
authenticated request and every WebSocket connect of a bound token must
present a fresh DPoP proof signed by the private half. A stolen JWT is
useless without the key, and the key cannot be exfiltrated by any
script: an XSS can abuse the *live* session while the tab is open, but
cannot steal a credential that outlives the reload.

Signature verification runs on ``cryptography`` (OpenSSL-backed — the
same linkage the FIPS inventory proves for the HS256 route) rather than
python-jose's EC route, which could silently bind to the pure-Python
``ecdsa`` package on hosts without the cryptography backend selected.

Proofs are one-shot: each carries a unique ``jti`` and a fresh ``iat``,
is replay-blocked for the freshness window, and its ``ath`` claim binds
it to the exact access token it accompanies. The ``htu`` check compares
the URI's *path* (query and scheme/host are ignored) so deployments
behind the Caddy reverse proxy — where the app may see a different
scheme or port than the browser used — do not reject honest proofs.
"""

import base64
import hashlib
import json
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

#: How far a proof's ``iat`` may sit from the server clock (seconds),
#: in either direction (clock-skew allowance), and how long its ``jti``
#: is remembered for replay blocking.
PROOF_WINDOW_SECONDS = 300

#: Cap on remembered proof JTIs; the map is purged of expired entries
#: on every insert, so the cap only bounds a same-second flood.
REPLAY_CACHE_MAX = 10_000

_EC_CURVE = "P-256"


def _b64url_decode(value: str) -> bytes:
    """Decode unpadded base64url (padding is re-added)."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(raw: bytes) -> str:
    """Encode as unpadded base64url (for tests that mint proofs)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _is_ec_p256(jwk: dict) -> bool:
    """True for an EC JWK on the P-256 curve with both coordinates."""
    if jwk.get("kty") != "EC":
        return False
    if jwk.get("crv") != _EC_CURVE:
        return False
    return "x" in jwk and "y" in jwk


def jwk_thumbprint(jwk: dict) -> str | None:
    """RFC 7638 thumbprint of an EC P-256 public JWK, or None.

    Only the required EC members (crv, kty, x, y) participate, in
    lexicographic order with compact separators, per the RFC.
    """
    if not _is_ec_p256(jwk):
        return None
    members = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"))
    return _b64url_encode(hashlib.sha256(canonical.encode()).digest())


def validate_public_jwk(jwk) -> str | None:
    """The thumbprint of *jwk* when it is a public EC P-256 JWK.

    None (refuse) for anything else: wrong key type or curve, missing
    coordinates, or a JWK that carries private key material (``d``) —
    a client must never upload the private half.
    """
    if not isinstance(jwk, dict) or "d" in jwk:
        return None
    return jwk_thumbprint(jwk)


def access_token_hash(access_token: str) -> str:
    """The ``ath`` claim value: unpadded base64url of the token digest."""
    return _b64url_encode(hashlib.sha256(access_token.encode()).digest())


def _der_int(value: int) -> bytes:
    """DER INTEGER encoding (minimal bytes, high-bit sign padding)."""
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + bytes([len(raw)]) + raw


def _raw_to_der(signature: bytes) -> bytes:
    """Convert WebCrypto's raw P1363 ``r||s`` signature to ASN.1 DER."""
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


def _ec_public_key(jwk: dict):
    """An ``ec`` public key from the JWK coordinates."""
    x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    numbers = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
    return numbers.public_key()


def verify_signature(
    jwk: dict, signing_input: bytes, signature: bytes
) -> bool:
    """ES256 verification of *signature* over *signing_input*.

    Accepts the JWS-standard DER signature or the raw 64-byte P1363
    form WebCrypto emits (the browser client sends the raw form).
    """
    try:
        key = _ec_public_key(jwk)
        der = _raw_to_der(signature) if len(signature) == 64 else signature
        key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def decode_proof(proof: str) -> tuple[dict, dict, bytes] | None:
    """Split a compact-serialization DPoP JWT into (header, payload, sig).

    None for a malformed proof: wrong part count, non-base64url or
    non-JSON segments, or non-object header/payload.
    """
    parts = proof.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, TypeError):
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    return header, payload, signature


def _proof_jkt(header: dict) -> str | None:
    """The thumbprint of the public JWK in the proof header.

    RFC 9449 §4.2: the header JWK must be public — private material
    (``d``) is refused exactly like at bind time.
    """
    jwk = header.get("jwk")
    if not isinstance(jwk, dict):
        return None
    return validate_public_jwk(jwk)


def _header_reason(header: dict, expected_jkt: str) -> str | None:
    if header.get("typ") != "dpop+jwt":
        return "wrong typ"
    if header.get("alg") != "ES256":
        return "wrong alg"
    if _proof_jkt(header) != expected_jkt:
        return "proof key does not match the token binding"
    return None


def _htu_path(htu) -> str | None:
    """The path component of the proof's ``htu`` URI (None when bad)."""
    if not isinstance(htu, str):
        return None
    try:
        return urlsplit(htu).path
    except ValueError:
        return None


def _claim_reason(payload: dict, method: str, path: str, token: str):
    """Binding claims: the proof names this request and this token."""
    if payload.get("htm") != method:
        return "method mismatch"
    if _htu_path(payload.get("htu")) != path:
        return "uri mismatch"
    if payload.get("ath") != access_token_hash(token):
        return "token hash mismatch"
    return None


def _freshness_reason(payload: dict, now: float, replay: dict) -> str | None:
    """Freshness + replay claims: a proof is single-use and short-lived."""
    iat = payload.get("iat")
    jti = payload.get("jti")
    if not isinstance(iat, (int, float)) or not isinstance(jti, str):
        return "malformed claims"
    if abs(now - iat) > PROOF_WINDOW_SECONDS:
        return "stale proof"
    if jti in replay:
        return "replayed proof"
    return None


def purge_replay(
    replay: dict, now: float, max_entries: int = REPLAY_CACHE_MAX
) -> None:
    """Drop expired JTIs, then trim to the cap (insertion-oldest first).

    *max_entries* is a parameter so tests can exercise the trim without
    mutating the module constant.
    """
    for jti, expires in list(replay.items()):
        if expires <= now:
            del replay[jti]
    excess = len(replay) - max_entries
    for jti in list(replay)[: max(0, excess)]:
        del replay[jti]


def _record_if_authentic(
    header: dict,
    payload: dict,
    signature: bytes,
    proof: str,
    replay: dict,
    now: float,
) -> str | None:
    """Verify the proof signature, then remember its JTI; None = success."""
    signing_input = proof.rsplit(".", 1)[0].encode()
    if not verify_signature(header["jwk"], signing_input, signature):
        return "bad signature"
    replay[payload["jti"]] = now + PROOF_WINDOW_SECONDS
    purge_replay(replay, now)
    return None


def verify_proof(
    proof,
    *,
    method: str,
    path: str,
    access_token: str,
    expected_jkt: str,
    now: float,
    replay: dict,
) -> str | None:
    """Verify a DPoP proof; None on success, a reason string on failure.

    *replay* is the caller-owned JTI→expiry map (the live server keeps
    one on ``app.state.auth``); a winning proof's JTI is recorded in it.
    """
    if not isinstance(proof, str):
        return "missing proof"
    decoded = decode_proof(proof)
    if decoded is None:
        return "malformed proof"
    header, payload, signature = decoded
    for reason in (
        _header_reason(header, expected_jkt),
        _claim_reason(payload, method, path, access_token),
        _freshness_reason(payload, now, replay),
    ):
        if reason is not None:
            return reason
    return _record_if_authentic(header, payload, signature, proof, replay, now)
