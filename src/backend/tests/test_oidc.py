"""Tests for OIDC client module."""

import time
from unittest.mock import AsyncMock, patch

import yaml

import pytest

from klangk_backend import oidc
from klangk_backend.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def clean_oidc_state(monkeypatch):
    """Reset OIDC module state between tests."""
    monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
    monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
    oidc._providers.clear()
    oidc.clear_caches()
    yield
    oidc._providers.clear()
    oidc.clear_caches()


def _provider(**overrides):
    defaults = {
        "id": "test",
        "display_name": "Test IdP",
        "issuer": "https://idp.example.com",
        "client_id": "klangk",
        "client_secret": "secret",
        "scopes": "openid email profile",
    }
    defaults.update(overrides)
    return oidc.OIDCProvider(**defaults)


class TestGet:
    def test_missing_key_raises(self):
        with pytest.raises(KeyError, match="missing-key"):
            oidc.get({}, "missing-key")


class TestLoadConfig:
    def test_no_config(self, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        assert oidc.load_config() == []

    def test_missing_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(tmp_path / "nope.json"))
        with pytest.raises(ConfigurationError, match="absolute path"):
            oidc.load_config()

    def test_valid_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "test",
                        "display-name": "Test",
                        "issuer": "https://idp.example.com/",
                        "client-id": "klangk",
                        "client-secret": "s3cret",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert len(providers) == 1
        assert providers[0].id == "test"
        assert providers[0].issuer == "https://idp.example.com"
        assert providers[0].client_secret == "s3cret"

    def test_file_secret(self, monkeypatch, tmp_path):
        secret_file = tmp_path / "secret"
        secret_file.write_text("file-secret\n")
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "fs",
                        "display-name": "File Secret",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": f"file:{secret_file}",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].client_secret == "file-secret"

    def test_ca_cert(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "dod",
                        "display-name": "DoD",
                        "issuer": "https://sso.mil/realms/dod",
                        "client-id": "klangk",
                        "client-secret": "s",
                        "ca-cert": "/etc/pki/dod-ca.pem",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].ca_cert == "/etc/pki/dod-ca.pem"

    def test_token_validation_pem(self, monkeypatch, tmp_path):
        pem = "-----BEGIN PUBLIC KEY-----\nMIIBI...\n-----END PUBLIC KEY-----"
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "dod",
                        "display-name": "DoD",
                        "issuer": "https://sso.mil/realms/dod",
                        "client-id": "klangk",
                        "client-secret": "s",
                        "token-validation-pem": pem,
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].token_validation_pem == pem

    def test_ca_cert_relative_resolved(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "rel",
                        "display-name": "Rel",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": "s",
                        "ca-cert": "certs/ca.pem",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        expected = str(tmp_path / "certs" / "ca.pem")
        assert providers[0].ca_cert == expected

    def test_ca_cert_default_none(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "test",
                        "display-name": "Test",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": "s",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].ca_cert is None

    def test_trust_email(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "trusted",
                        "display-name": "Trusted",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": "s",
                        "trust-email": True,
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].trust_email is True

    def test_trust_email_defaults_false(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "default",
                        "display-name": "Default",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": "s",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert providers[0].trust_email is False

    def test_multiple_providers(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "a",
                        "display-name": "A",
                        "issuer": "https://a.example.com",
                        "client-id": "klangk",
                        "client-secret": "sa",
                    },
                    {
                        "id": "b",
                        "display-name": "B",
                        "issuer": "https://b.example.com",
                        "client-id": "klangk",
                        "client-secret": "sb",
                    },
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert len(providers) == 2
        assert providers[0].id == "a"
        assert providers[1].id == "b"

    def test_snake_case_fallback(self, monkeypatch, tmp_path):
        """Legacy snake_case keys still work for backwards compat."""
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "legacy",
                        "display_name": "Legacy",
                        "issuer": "https://idp.example.com/",
                        "client_id": "klangk",
                        "client_secret": "s3cret",
                        "ca_cert": "/etc/ca.pem",
                        "token_validation_pem": "pem-data",
                        "logout_redirect": True,
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        providers = oidc.load_config()
        assert len(providers) == 1
        assert providers[0].display_name == "Legacy"
        assert providers[0].client_id == "klangk"
        assert providers[0].client_secret == "s3cret"
        assert providers[0].ca_cert == "/etc/ca.pem"
        assert providers[0].token_validation_pem == "pem-data"
        assert providers[0].logout_redirect is True


class TestInlineProviders:
    """OIDC providers specified inline in the klangkd config file (#1395)."""

    def test_inline_providers_loaded(self, tmp_path):
        from klangk_backend.settings import set_config_file

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "oidc_providers:\n"
            "  - id: inline\n"
            '    display-name: "Inline IdP"\n'
            "    issuer: https://idp.example.com\n"
            "    client-id: klangk\n"
            '    client-secret: "inline-secret"\n'
        )
        set_config_file(str(cfg))
        try:
            providers = oidc.load_config()
            assert len(providers) == 1
            assert providers[0].id == "inline"
            assert providers[0].display_name == "Inline IdP"
            assert providers[0].client_secret == "inline-secret"
            assert providers[0].issuer == "https://idp.example.com"
        finally:
            set_config_file(None)

    def test_external_file_overrides_inline(self, monkeypatch, tmp_path):
        """When both inline and external are configured, external wins
        (env var override, consistent with env > file precedence)."""
        from klangk_backend.settings import set_config_file

        # External file via env var
        ext = tmp_path / "oidc.yaml"
        ext.write_text(
            yaml.dump(
                [
                    {
                        "id": "external",
                        "display-name": "External",
                        "issuer": "https://ext.example.com",
                        "client-id": "klangk",
                        "client-secret": "ext",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(ext))

        # Inline in config file
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "oidc_providers:\n"
            "  - id: inline\n"
            '    display-name: "Inline"\n'
            "    issuer: https://inline.example.com\n"
            "    client-id: klangk\n"
            '    client-secret: "inline"\n'
        )
        set_config_file(str(cfg))
        try:
            providers = oidc.load_config()
            assert len(providers) == 1
            assert providers[0].id == "external"
        finally:
            set_config_file(None)

    def test_inline_file_secret_resolution(self, tmp_path):
        """file: prefix in inline provider secrets resolves correctly."""
        from klangk_backend.settings import set_config_file

        secret = tmp_path / "secret.txt"
        secret.write_text("resolved-secret\n")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "oidc_providers:\n"
            "  - id: fs\n"
            '    display-name: "File Secret"\n'
            "    issuer: https://idp.example.com\n"
            "    client-id: klangk\n"
            f'    client-secret: "file:{secret}"\n'
        )
        set_config_file(str(cfg))
        try:
            providers = oidc.load_config()
            assert providers[0].client_secret == "resolved-secret"
        finally:
            set_config_file(None)

    def test_inline_multiple_providers(self, tmp_path):
        from klangk_backend.settings import set_config_file

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "oidc_providers:\n"
            "  - id: a\n"
            '    display-name: "A"\n'
            "    issuer: https://a.example.com\n"
            "    client-id: klangk\n"
            '    client-secret: "sa"\n'
            "  - id: b\n"
            '    display-name: "B"\n'
            "    issuer: https://b.example.com\n"
            "    client-id: klangk\n"
            '    client-secret: "sb"\n'
        )
        set_config_file(str(cfg))
        try:
            providers = oidc.load_config()
            assert len(providers) == 2
            assert providers[0].id == "a"
            assert providers[1].id == "b"
        finally:
            set_config_file(None)

    def test_falls_back_to_external_when_no_inline(
        self, monkeypatch, tmp_path
    ):
        """With no inline providers, external file is used."""
        ext = tmp_path / "oidc.yaml"
        ext.write_text(
            yaml.dump(
                [
                    {
                        "id": "external",
                        "display-name": "External",
                        "issuer": "https://ext.example.com",
                        "client-id": "klangk",
                        "client-secret": "ext",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(ext))
        providers = oidc.load_config()
        assert len(providers) == 1
        assert providers[0].id == "external"


class TestProviderRegistry:
    def test_init_and_lookup(self, monkeypatch, tmp_path):
        cfg = tmp_path / "oidc.yaml"
        cfg.write_text(
            yaml.dump(
                [
                    {
                        "id": "test",
                        "display-name": "Test",
                        "issuer": "https://idp.example.com",
                        "client-id": "klangk",
                        "client-secret": "s",
                    }
                ]
            )
        )
        monkeypatch.setenv("KLANGK_OIDC_CONFIG", str(cfg))
        oidc.init_providers()
        assert oidc.is_enabled()
        assert oidc.get_provider("test") is not None
        assert oidc.get_provider("nope") is None
        providers = oidc.list_providers()
        assert providers == [{"id": "test", "display_name": "Test"}]

    def test_not_enabled_when_empty(self):
        assert not oidc.is_enabled()
        assert oidc.list_providers() == []

    def test_init_raises_when_oidc_required_but_no_providers(
        self, monkeypatch
    ):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "oidc")
        with pytest.raises(ConfigurationError, match="no OIDC providers"):
            oidc.init_providers()

    def test_init_raises_when_both_required_but_no_providers(
        self, monkeypatch
    ):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "both")
        with pytest.raises(ConfigurationError, match="no OIDC providers"):
            oidc.init_providers()

    def test_init_ok_when_password_only(self, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.setenv("KLANGK_AUTH_MODES", "password")
        oidc.init_providers()
        assert not oidc.is_enabled()


class TestAuthModes:
    def test_default_none_when_no_oidc(self, monkeypatch):
        # Production default (no OIDC, mode unset) is now ``none`` — a fresh
        # klangk boots in no-login single-user local-dev mode (#1374).
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        assert oidc.auth_modes() == "none"
        assert not oidc.password_login_allowed()
        assert oidc.local_login_allowed()

    def test_default_none_when_oidc_enabled(self, monkeypatch):
        # #1419: OIDC settings no longer change auth_mode. An unset
        # KLANGK_AUTH_MODES resolves to "none" even when an OIDC provider
        # is configured — OIDC is opt-in via an explicit
        # KLANGK_AUTH_MODES=oidc (or "both"). (Earlier versions promoted
        # the unset default to "both" here; that rule was removed.)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        oidc._providers.append(_provider())
        assert oidc.auth_modes() == "none"
        assert oidc.local_login_allowed()
        assert not oidc.password_login_allowed()
        assert not oidc.oidc_login_allowed()

    def test_oidc_only(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "oidc")
        oidc._providers.append(_provider())
        assert oidc.auth_modes() == "oidc"
        assert not oidc.password_login_allowed()
        assert oidc.oidc_login_allowed()

    def test_password_only(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "password")
        oidc._providers.append(_provider())
        assert oidc.auth_modes() == "password"
        assert oidc.password_login_allowed()
        assert not oidc.oidc_login_allowed()

    def test_none_mode(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        assert oidc.auth_modes() == "none"
        assert not oidc.password_login_allowed()
        assert not oidc.oidc_login_allowed()
        assert oidc.local_login_allowed()

    def test_none_mode_ignores_oidc_config(self, monkeypatch):
        # OIDC config may be present but is irrelevant in none mode.
        oidc._providers.append(_provider())
        monkeypatch.setenv("KLANGK_AUTH_MODES", "none")
        assert oidc.auth_modes() == "none"
        assert oidc.local_login_allowed()

    def test_local_login_false_in_other_modes(self, monkeypatch):
        for mode in ("password", "oidc", "both"):
            monkeypatch.setenv("KLANGK_AUTH_MODES", mode)
            assert not oidc.local_login_allowed()

    # --- #1397 preset-driven default (KLANGK_AUTH_MODES unset) ---
    # A ``*-auth`` preset means the gate is required, so the *unset* default
    # resolves to ``password`` (the operator still picks the backend —
    # e.g. OIDC — by setting KLANGK_AUTH_MODES explicitly). A ``*-noauth``
    # preset stays ``none``. With no preset set, today's pre-#1392 default
    # (the OIDC-promotion rule below) is preserved — removing that promotion
    # is a separate breaking change, deferred to #1392 chunk 7.

    @pytest.mark.parametrize("preset", ["uds-auth", "ip-auth"])
    def test_auth_preset_defaults_to_password(self, monkeypatch, preset):
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.setenv("KLANGK_PRESET", preset)
        assert oidc.auth_modes() == "password"
        assert oidc.password_login_allowed()
        assert not oidc.local_login_allowed()

    @pytest.mark.parametrize("preset", ["uds-noauth", "ip-noauth"])
    def test_noauth_preset_defaults_to_none(self, monkeypatch, preset):
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.setenv("KLANGK_PRESET", preset)
        assert oidc.auth_modes() == "none"
        assert oidc.local_login_allowed()
        assert not oidc.password_login_allowed()

    def test_auth_preset_default_does_not_override_explicit_mode(
        self, monkeypatch
    ):
        # The preset only owns the *default* — an explicit KLANGK_AUTH_MODES
        # always wins (even a conflicting one; that's what the settings-level
        # _validate_preset cross-check is for, not auth_modes() itself).
        monkeypatch.setenv("KLANGK_PRESET", "uds-auth")
        monkeypatch.setenv("KLANGK_AUTH_MODES", "oidc")
        oidc._providers.append(_provider())
        assert oidc.auth_modes() == "oidc"
        assert oidc.oidc_login_allowed()
        assert not oidc.password_login_allowed()

    def test_no_preset_preserves_legacy_default(self, monkeypatch):
        # No preset + AUTH_MODES unset → ``none``. OIDC settings no longer
        # promote the mode (#1419), so configuring a provider does not flip
        # the default; the operator must set KLANGK_AUTH_MODES=oidc/both.
        monkeypatch.delenv("KLANGK_PRESET", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        assert oidc.auth_modes() == "none"
        oidc._providers.append(_provider())
        assert oidc.auth_modes() == "none"


class TestClientKwargs:
    def test_no_ca_cert(self):
        provider = _provider()
        assert oidc.client_kwargs(provider) == {}

    def test_with_ca_cert(self):
        provider = _provider(ca_cert="/etc/pki/ca.pem")
        assert oidc.client_kwargs(provider) == {"verify": "/etc/pki/ca.pem"}


class TestPKCE:
    def test_generate_pkce(self):
        verifier, challenge = oidc.generate_pkce()
        assert len(verifier) > 32
        assert len(challenge) > 32
        assert verifier != challenge

    def test_pkce_challenge_is_s256(self):
        import base64
        import hashlib

        verifier, challenge = oidc.generate_pkce()
        expected = (
            base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        assert challenge == expected


def _mock_httpx_client(get_response=None, post_response=None):
    """Create a mock httpx.AsyncClient that works as an async context manager."""
    client = AsyncMock()
    if get_response:
        client.get.return_value = get_response
    if post_response:
        client.post.return_value = post_response
    # httpx.AsyncClient.__aenter__ returns self
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _mock_response(data):
    """Create a mock httpx response with sync .json() and .raise_for_status()."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


class TestDiscovery:
    async def test_discover_caches(self):
        provider = _provider()
        disc_data = {
            "authorization_endpoint": "https://idp.example.com/auth",
            "token_endpoint": "https://idp.example.com/token",
            "jwks_uri": "https://idp.example.com/jwks",
        }
        mock_resp = _mock_response(disc_data)
        client_instance = _mock_httpx_client(get_response=mock_resp)

        with patch("httpx.AsyncClient", return_value=client_instance):
            result1 = await oidc.discover(provider)
            assert result1 == disc_data

            # Second call should use cache
            result2 = await oidc.discover(provider)
            assert result2 == disc_data
            # Only one HTTP call
            assert client_instance.get.call_count == 1

    async def test_discover_cache_expires(self):
        provider = _provider()
        disc_data = {"authorization_endpoint": "https://idp.example.com/auth"}
        mock_resp = _mock_response(disc_data)
        client_instance = _mock_httpx_client(get_response=mock_resp)

        with patch("httpx.AsyncClient", return_value=client_instance):
            await oidc.discover(provider)
            # Expire the cache
            oidc._discovery_cache[provider.id].fetched_at = (
                time.time() - oidc._DISCOVERY_TTL - 1
            )
            await oidc.discover(provider)
            assert client_instance.get.call_count == 2


class TestBuildAuthUrl:
    async def test_build_auth_url(self):
        provider = _provider()
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={
                "authorization_endpoint": "https://idp.example.com/auth",
            },
            fetched_at=time.time(),
        )
        url = await oidc.build_auth_url(
            provider,
            "https://klangk.example.com/callback",
            "state123",
            "challenge456",
        )
        assert url.startswith("https://idp.example.com/auth?")
        assert "client_id=klangk" in url
        assert "state=state123" in url
        assert "code_challenge=challenge456" in url
        assert "code_challenge_method=S256" in url


class TestExchangeCode:
    async def test_exchange_code(self):
        provider = _provider()
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={"token_endpoint": "https://idp.example.com/token"},
            fetched_at=time.time(),
        )
        token_resp = {"access_token": "at", "id_token": "idt"}
        mock_resp = _mock_response(token_resp)
        client_instance = _mock_httpx_client(post_response=mock_resp)

        with patch("httpx.AsyncClient", return_value=client_instance):
            result = await oidc.exchange_code(
                provider, "code123", "https://cb", "verifier"
            )
            assert result == token_resp
            call_data = client_instance.post.call_args[1]["data"]
            assert call_data["code"] == "code123"
            assert call_data["code_verifier"] == "verifier"
            assert call_data["client_secret"] == "secret"


class TestGetJWKS:
    async def test_get_jwks_caches(self):
        provider = _provider()
        # Pre-populate discovery cache
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={"jwks_uri": "https://idp.example.com/jwks"},
            fetched_at=time.time(),
        )
        jwks_data = {"keys": [{"kty": "RSA", "kid": "key1"}]}
        mock_resp = _mock_response(jwks_data)
        client_instance = _mock_httpx_client(get_response=mock_resp)

        with patch("httpx.AsyncClient", return_value=client_instance):
            result1 = await oidc.get_jwks(provider)
            assert result1 == jwks_data
            result2 = await oidc.get_jwks(provider)
            assert result2 == jwks_data
            assert client_instance.get.call_count == 1

    async def test_get_jwks_cache_expires(self):
        provider = _provider()
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={"jwks_uri": "https://idp.example.com/jwks"},
            fetched_at=time.time(),
        )
        jwks_data = {"keys": []}
        mock_resp = _mock_response(jwks_data)
        client_instance = _mock_httpx_client(get_response=mock_resp)

        with patch("httpx.AsyncClient", return_value=client_instance):
            await oidc.get_jwks(provider)
            oidc._jwks_cache[provider.id].fetched_at = (
                time.time() - oidc._JWKS_TTL - 1
            )
            await oidc.get_jwks(provider)
            assert client_instance.get.call_count == 2


class TestValidateIdToken:
    async def test_validate_id_token_with_jwks(self):
        from unittest.mock import MagicMock

        provider = _provider()
        claims = {"sub": "user1", "email": "user@example.com"}
        with (
            patch.object(
                oidc, "get_jwks", AsyncMock(return_value={"keys": []})
            ) as mock_jwks,
            patch.object(
                oidc.jose_jwt,
                "decode",
                MagicMock(return_value=claims),
            ) as mock_decode,
        ):
            result = await oidc.validate_id_token(
                provider, "fake-token", access_token="fake-at"
            )
            assert result == claims
            mock_jwks.assert_awaited_once()
            mock_decode.assert_called_once_with(
                "fake-token",
                {"keys": []},
                algorithms=["RS256", "ES256"],
                audience="klangk",
                issuer="https://idp.example.com",
                access_token="fake-at",
            )

    async def test_validate_id_token_with_static_pem(self):
        from unittest.mock import MagicMock

        pem = "-----BEGIN PUBLIC KEY-----\nMIIBI...\n-----END PUBLIC KEY-----"
        provider = _provider(token_validation_pem=pem)
        claims = {"sub": "user1", "email": "user@example.com"}
        with (
            patch.object(oidc, "get_jwks", AsyncMock()) as mock_jwks,
            patch.object(
                oidc.jose_jwt,
                "decode",
                MagicMock(return_value=claims),
            ) as mock_decode,
        ):
            result = await oidc.validate_id_token(provider, "fake-token")
            assert result == claims
            mock_jwks.assert_not_awaited()
            mock_decode.assert_called_once_with(
                "fake-token",
                pem,
                algorithms=["RS256", "ES256"],
                audience="klangk",
                issuer="https://idp.example.com",
                access_token=None,
            )


class TestBuildLogoutUrl:
    async def test_disabled(self):
        provider = _provider(logout_redirect=False)
        result = await oidc.build_logout_url(provider, "https://klangk/login")
        assert result is None

    async def test_no_end_session_endpoint(self):
        provider = _provider(logout_redirect=True)
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={"authorization_endpoint": "https://idp/auth"},
            fetched_at=time.time(),
        )
        result = await oidc.build_logout_url(provider, "https://klangk/login")
        assert result is None

    async def test_builds_url(self):
        provider = _provider(logout_redirect=True)
        oidc._discovery_cache[provider.id] = oidc.CachedDiscovery(
            data={
                "end_session_endpoint": "https://idp.example.com/logout",
            },
            fetched_at=time.time(),
        )
        result = await oidc.build_logout_url(
            provider, "https://klangk.example.com/#/login"
        )
        assert result is not None
        assert result.startswith("https://idp.example.com/logout?")
        assert "client_id=klangk" in result
        assert "post_logout_redirect_uri=" in result


class TestParseHookValue:
    def test_path_with_func(self):
        path, func = oidc._parse_hook_value("/etc/klangk/hook.py:my_func")
        assert path == "/etc/klangk/hook.py"
        assert func == "my_func"

    def test_path_without_func(self):
        path, func = oidc._parse_hook_value("/etc/klangk/hook.py")
        assert path == "/etc/klangk/hook.py"
        assert func == "on_login"

    def test_colon_in_path(self):
        path, func = oidc._parse_hook_value("/a/b:c/hook.py:check")
        assert path == "/a/b:c/hook.py"
        assert func == "check"


class TestLoadLoginHook:
    def test_no_hook_when_not_set(self, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_LOGIN_HOOK", raising=False)
        oidc.load_login_hook()
        assert oidc._login_hook is None

    def test_hook_loaded_from_file(self, monkeypatch, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def on_login(provider, claims, email, tokens):\n"
            "    return {'testers'}\n"
        )
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", str(hook_file))
        oidc.load_login_hook()
        assert oidc._login_hook is not None
        assert oidc._login_hook(None, {}, "", {}) == {"testers"}

    def test_hook_loaded_with_custom_func_name(self, monkeypatch, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def check(provider, claims, email, tokens):\n"
            "    return {'admins'}\n"
        )
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", f"{hook_file}:check")
        oidc.load_login_hook()
        assert oidc._login_hook is not None
        assert oidc._login_hook(None, {}, "", {}) == {"admins"}

    def test_async_hook_detected(self, monkeypatch, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "async def on_login(provider, claims, email, tokens):\n"
            "    return None\n"
        )
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", str(hook_file))
        oidc.load_login_hook()
        assert oidc._login_hook_is_async is True

    def test_file_not_found(self, monkeypatch):
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", "/nonexistent/hook.py")
        with pytest.raises(ConfigurationError, match="file not found"):
            oidc.load_login_hook()

    def test_func_not_found(self, monkeypatch, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text("x = 1\n")
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", f"{hook_file}:missing")
        with pytest.raises(
            ConfigurationError, match="not found or not callable"
        ):
            oidc.load_login_hook()

    def test_not_callable(self, monkeypatch, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text("on_login = 42\n")
        monkeypatch.setenv("KLANGK_OIDC_LOGIN_HOOK", str(hook_file))
        with pytest.raises(
            ConfigurationError, match="not found or not callable"
        ):
            oidc.load_login_hook()


class TestCallLoginHook:
    async def test_no_hook_returns_none(self):
        oidc._login_hook = None
        result = await oidc.call_login_hook(
            _provider(), {}, "x@example.com", {}
        )
        assert result is None

    async def test_hook_returns_groups(self):
        def hook(provider, claims, email, tokens):
            return {"admin", "devs"}

        oidc._login_hook = hook
        oidc._login_hook_is_async = False
        result = await oidc.call_login_hook(
            _provider(), {}, "x@example.com", {}
        )
        assert result == {"admin", "devs"}

    async def test_hook_returns_none(self):
        def hook(provider, claims, email, tokens):
            return None

        oidc._login_hook = hook
        oidc._login_hook_is_async = False
        result = await oidc.call_login_hook(
            _provider(), {}, "x@example.com", {}
        )
        assert result is None

    async def test_async_hook_returns_groups(self):
        async def hook(provider, claims, email, tokens):
            return {"editors"}

        oidc._login_hook = hook
        oidc._login_hook_is_async = True
        result = await oidc.call_login_hook(
            _provider(), {}, "x@example.com", {}
        )
        assert result == {"editors"}

    async def test_hook_raises_rejects_login(self):
        def hook(provider, claims, email, tokens):
            raise ValueError("denied")

        oidc._login_hook = hook
        oidc._login_hook_is_async = False
        with pytest.raises(ValueError, match="denied"):
            await oidc.call_login_hook(_provider(), {}, "x@example.com", {})

    async def test_async_hook_raises_rejects_login(self):
        async def hook(provider, claims, email, tokens):
            raise ValueError("async denied")

        oidc._login_hook = hook
        oidc._login_hook_is_async = True
        with pytest.raises(ValueError, match="async denied"):
            await oidc.call_login_hook(_provider(), {}, "x@example.com", {})


class TestSyncOidcGroups:
    async def test_creates_groups_and_adds_memberships(self, db):
        from klangk_backend import model

        user = await model.create_user("sync2@example.com", "hash")
        await oidc.sync_oidc_groups(user["id"], {"new-group-a", "new-group-b"})
        groups = await model.get_user_groups(user["id"])
        names = {g["name"] for g in groups}
        assert "new-group-a" in names
        assert "new-group-b" in names
        sync_ids = await model.get_user_oidc_sync_group_ids(user["id"])
        assert len(sync_ids) == 2

    async def test_removes_stale_oidc_sync(self, db):
        from klangk_backend import model

        user = await model.create_user("sync3@example.com", "hash")
        group = await model.create_group("old-group")
        await model.add_user_to_group(user["id"], group["id"], "oidc_sync")

        await oidc.sync_oidc_groups(user["id"], set())
        assert await model.get_user_oidc_sync_group_ids(user["id"]) == []

    async def test_preserves_manual_memberships(self, db):
        from klangk_backend import model

        user = await model.create_user("sync4@example.com", "hash")
        group = await model.create_group("manual-group")
        await model.add_user_to_group(user["id"], group["id"], "manual")

        await oidc.sync_oidc_groups(user["id"], set())
        all_ids = await model.get_user_group_ids(user["id"])
        assert group["id"] in all_ids
