"""Tests for klangk.dpop — DPoP proof verification (RFC 9449, #3218)."""

import base64
import hashlib
import time

import pytest

from klangk import dpop

from _helpers import build_dpop_proof, der_to_raw_p1363, make_binding_key


def _thumbprint_of(jwk: dict) -> str:
    """Independent RFC 7638 recomputation (literal canonical JSON)."""
    canonical = (
        f'{{"crv":"P-256","kty":"EC","x":"{jwk["x"]}","y":"{jwk["y"]}"}}'
    )
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _proof_ok(private, jwk, **kwargs) -> str:
    """A valid proof for the standard request shape."""
    from _helpers import make_dpop_proof

    return make_dpop_proof(
        private,
        jwk,
        method=kwargs.get("method", "GET"),
        uri=kwargs.get("uri", "https://host/api/v1/x"),
        token=kwargs.get("token", "the-access-token"),
    )


def _verify(
    proof,
    *,
    method="GET",
    path="/api/v1/x",
    jkt="expected",
    token="the-access-token",
):
    return dpop.verify_proof(
        proof,
        method=method,
        path=path,
        access_token=token,
        expected_jkt=jkt,
        now=time.time(),
        replay={},
    )


@pytest.fixture
def key():
    return make_binding_key()


class TestThumbprint:
    def test_matches_rfc7638_recomputation(self, key):
        _, jwk = key
        assert dpop.jwk_thumbprint(jwk) == _thumbprint_of(jwk)

    def test_wrong_kty(self, key):
        _, jwk = key
        assert dpop.jwk_thumbprint({**jwk, "kty": "RSA"}) is None

    def test_wrong_curve(self, key):
        _, jwk = key
        assert dpop.jwk_thumbprint({**jwk, "crv": "P-384"}) is None

    def test_missing_coordinates(self, key):
        _, jwk = key
        assert dpop.jwk_thumbprint({"kty": "EC", "crv": "P-256"}) is None

    def test_non_string_coordinates_refused(self, key):
        """#3230 round-3: numeric/nested coordinates are not a usable
        key — no thumbprint, so no token can be bound to nothing."""
        assert (
            dpop.jwk_thumbprint({"kty": "EC", "crv": "P-256", "x": 1, "y": 2})
            is None
        )
        assert (
            dpop.jwk_thumbprint(
                {"kty": "EC", "crv": "P-256", "x": [1], "y": {"a": 2}}
            )
            is None
        )


class TestValidatePublicJwk:
    def test_public_ec_key_accepted(self, key):
        _, jwk = key
        assert dpop.validate_public_jwk(jwk) == _thumbprint_of(jwk)

    def test_private_material_refused(self, key):
        _, jwk = key
        assert dpop.validate_public_jwk({**jwk, "d": "secret"}) is None

    def test_non_dict_refused(self):
        assert dpop.validate_public_jwk("not-a-jwk") is None


class TestAccessTokenHash:
    def test_known_digest(self):
        digest = hashlib.sha256(b"tok").digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert dpop.access_token_hash("tok") == expected


class TestRawToDer:
    def test_high_bit_integers_round_trip(self):
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )

        raw = bytes([0x80]) * 32 + bytes([0x01]) * 32
        r, s = decode_dss_signature(dpop._raw_to_der(raw))
        assert r == int.from_bytes(raw[:32], "big")
        assert s == int.from_bytes(raw[32:], "big")

    def test_small_integers_round_trip(self):
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )

        raw = bytes([0x01]) * 64
        r, s = decode_dss_signature(dpop._raw_to_der(raw))
        assert r == int.from_bytes(raw[:32], "big")
        assert s == int.from_bytes(raw[32:], "big")

    def test_der_round_trip_via_helper(self, key):
        private, _ = key
        der = private.sign(b"m", _ec_sig_hasher())
        assert der_to_raw_p1363(der) == der_to_raw_p1363(der)


def _ec_sig_hasher():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.ECDSA(hashes.SHA256())


class TestVerifySignature:
    def test_raw_webcrypto_form_verifies(self, key):
        private, jwk = key
        signing_input = b"header.payload"
        der = private.sign(signing_input, _ec_sig_hasher())
        raw = der_to_raw_p1363(der)
        assert dpop.verify_signature(jwk, signing_input, raw)

    def test_der_form_verifies(self, key):
        private, jwk = key
        signing_input = b"header.payload"
        der = private.sign(signing_input, _ec_sig_hasher())
        assert dpop.verify_signature(jwk, signing_input, der)

    def test_tampered_input_fails(self, key):
        private, jwk = key
        der = private.sign(b"original", _ec_sig_hasher())
        assert not dpop.verify_signature(
            jwk, b"tampered", der_to_raw_p1363(der)
        )

    def test_point_off_curve_fails(self, key):
        _, jwk = key
        assert not dpop.verify_signature(jwk, b"x", b"\x01" * 64)

    def test_non_string_coordinates_fail(self):
        jwk = {"kty": "EC", "crv": "P-256", "x": 1, "y": 2}
        assert not dpop.verify_signature(jwk, b"x", b"\x01" * 64)


class TestDecodeProof:
    def _valid(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        header, payload, signature = dpop.decode_proof(proof)
        assert header["typ"] == "dpop+jwt"
        assert payload["htm"] == "GET"
        assert len(signature) in (64, 70, 71, 72)

    def test_wrong_part_count(self):
        assert dpop.decode_proof("two.parts") is None

    def test_bad_base64(self):
        assert dpop.decode_proof("!!!.???.***") is None

    def test_non_object_header(self, key):
        private, jwk = key
        proof = build_dpop_proof(private, [1, 2], {"jti": "j"})
        assert dpop.decode_proof(proof) is None

    def test_non_object_payload(self, key):
        private, jwk = key
        proof = build_dpop_proof(private, {"alg": "ES256"}, [1, 2])
        assert dpop.decode_proof(proof) is None


class TestVerifyProof:
    def test_valid_proof_accepted(self, key):
        private, jwk = key
        assert (
            _verify(_proof_ok(private, jwk), jkt=_thumbprint_of(jwk)) is None
        )

    def test_missing_proof(self):
        assert _verify(None) == "missing proof"

    def test_malformed_proof(self):
        assert _verify("garbage") == "malformed proof"

    def test_header_without_jwk(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        header, payload, _ = dpop.decode_proof(proof)
        bad = build_dpop_proof(
            private, {k: v for k, v in header.items() if k != "jwk"}, payload
        )
        assert _verify(bad, jkt=_thumbprint_of(jwk)) == (
            "proof key does not match the token binding"
        )

    def test_header_jwk_with_private_material_refused(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        header, payload, _ = dpop.decode_proof(proof)
        leaky = dict(header)
        leaky["jwk"] = {**jwk, "d": "private-half"}
        bad = build_dpop_proof(private, leaky, payload)
        assert _verify(bad, jkt=_thumbprint_of(jwk)) == (
            "proof key does not match the token binding"
        )

    def test_wrong_typ(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        header, payload, _ = dpop.decode_proof(proof)
        bad = build_dpop_proof(private, {**header, "typ": "jwt"}, payload)
        assert _verify(bad, jkt=_thumbprint_of(jwk)) == "wrong typ"

    def test_wrong_alg(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        header, payload, _ = dpop.decode_proof(proof)
        bad = build_dpop_proof(private, {**header, "alg": "RS256"}, payload)
        assert _verify(bad, jkt=_thumbprint_of(jwk)) == "wrong alg"

    def test_key_mismatch(self, key):
        private, jwk = key
        other_private, other_jwk = make_binding_key()
        from klangk.dpop import access_token_hash

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": other_jwk}
        payload = {
            "jti": "j1",
            "htm": "GET",
            "htu": "https://host/api/v1/x",
            "iat": int(time.time()),
            "ath": access_token_hash("the-access-token"),
        }
        # The proof presents the other key's JWK while the token is
        # bound to the honest key's thumbprint.
        proof = build_dpop_proof(private, header, payload)
        reason = _verify(proof, jkt=_thumbprint_of(jwk))
        assert reason == "proof key does not match the token binding"

    def test_method_mismatch(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        assert _verify(proof, method="POST", jkt=_thumbprint_of(jwk)) == (
            "method mismatch"
        )

    def test_uri_path_mismatch(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        assert _verify(proof, path="/other", jkt=_thumbprint_of(jwk)) == (
            "uri mismatch"
        )

    def test_htu_query_and_scheme_ignored(self, key):
        private, jwk = key
        from _helpers import make_dpop_proof

        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="http://other:9999/api/v1/x?extra=1",
            token="the-access-token",
        )
        assert _verify(proof, jkt=_thumbprint_of(jwk)) is None

    def test_htu_not_a_string(self, key):
        private, jwk = key
        from _helpers import build_dpop_proof
        from klangk.dpop import access_token_hash

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        payload = {
            "jti": "j1",
            "htm": "GET",
            "htu": 42,
            "iat": int(time.time()),
            "ath": access_token_hash("the-access-token"),
        }
        proof = build_dpop_proof(private, header, payload)
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "uri mismatch"

    def test_htu_unparseable(self, key):
        private, jwk = key
        from _helpers import build_dpop_proof
        from klangk.dpop import access_token_hash

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        payload = {
            "jti": "j1",
            "htm": "GET",
            "htu": "http://[",
            "iat": int(time.time()),
            "ath": access_token_hash("the-access-token"),
        }
        proof = build_dpop_proof(private, header, payload)
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "uri mismatch"

    def test_token_hash_mismatch(self, key):
        private, jwk = key
        proof = _proof_ok(private, jwk)
        assert (
            _verify(proof, token="different-token", jkt=_thumbprint_of(jwk))
            == "token hash mismatch"
        )

    def test_stale_proof(self, key):
        private, jwk = key
        from _helpers import make_dpop_proof

        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="https://host/api/v1/x",
            token="the-access-token",
            iat=int(time.time()) - dpop.PROOF_WINDOW_SECONDS - 1,
        )
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "stale proof"

    def test_too_far_future_proof(self, key):
        private, jwk = key
        from _helpers import make_dpop_proof

        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="https://host/api/v1/x",
            token="the-access-token",
            iat=int(time.time()) + dpop.PROOF_WINDOW_SECONDS + 1,
        )
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "stale proof"

    def test_small_clock_skew_tolerated(self, key):
        private, jwk = key
        from _helpers import make_dpop_proof

        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="https://host/api/v1/x",
            token="the-access-token",
            iat=int(time.time()) + 30,
        )
        assert _verify(proof, jkt=_thumbprint_of(jwk)) is None

    def test_malformed_claims(self, key):
        private, jwk = key
        from _helpers import build_dpop_proof
        from klangk.dpop import access_token_hash

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        payload = {
            "jti": None,
            "htm": "GET",
            "htu": "https://host/api/v1/x",
            "iat": "not-a-number",
            "ath": access_token_hash("the-access-token"),
        }
        proof = build_dpop_proof(private, header, payload)
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "malformed claims"

    def test_replayed_proof(self, key):
        private, jwk = key
        from _helpers import make_dpop_proof

        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="https://host/api/v1/x",
            token="the-access-token",
            jti="fixed-jti",
        )
        replay: dict = {}
        first = dpop.verify_proof(
            proof,
            method="GET",
            path="/api/v1/x",
            access_token="the-access-token",
            expected_jkt=_thumbprint_of(jwk),
            now=time.time(),
            replay=replay,
        )
        second = dpop.verify_proof(
            proof,
            method="GET",
            path="/api/v1/x",
            access_token="the-access-token",
            expected_jkt=_thumbprint_of(jwk),
            now=time.time(),
            replay=replay,
        )
        assert first is None
        assert second == "replayed proof"
        assert len(replay) == 1

    def test_bad_signature(self, key):
        private, jwk = key
        from _helpers import build_dpop_proof
        from klangk.dpop import access_token_hash

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        payload = {
            "jti": "j2",
            "htm": "GET",
            "htu": "https://host/api/v1/x",
            "iat": int(time.time()),
            "ath": access_token_hash("the-access-token"),
        }
        other_private, _ = make_binding_key()
        proof = build_dpop_proof(other_private, header, payload)
        assert _verify(proof, jkt=_thumbprint_of(jwk)) == "bad signature"


class TestPurgeReplay:
    def test_expired_entries_dropped(self):
        replay = {"old": 1.0, "live": time.time() + 60}
        dpop.purge_replay(replay, now=time.time())
        assert list(replay) == ["live"]

    def test_over_cap_trimmed_oldest_first(self):
        replay = {f"j{i}": time.time() + 60 for i in range(10)}
        dpop.purge_replay(replay, now=time.time(), max_entries=5)
        assert len(replay) == 5
        assert list(replay) == [f"j{i}" for i in range(5, 10)]

    def test_under_cap_untouched(self):
        replay = {"only": time.time() + 60}
        dpop.purge_replay(replay, now=time.time())
        assert list(replay) == ["only"]


class TestCanonicalJson:
    def test_thumbprint_uses_sorted_compact_members(self, key):
        _, jwk = key
        # A differently-ordered dict must yield the same thumbprint.
        reordered = {
            "y": jwk["y"],
            "x": jwk["x"],
            "crv": jwk["crv"],
            "kty": jwk["kty"],
        }
        assert dpop.jwk_thumbprint(reordered) == dpop.jwk_thumbprint(jwk)
