"""Tests for runtime SSL/CA certificate trust (#1181).

Covers the shared resolver (:meth:`ssl_trust.SSLTrust.ssl_cert_dir`), the
backend-process trust path (:meth:`ssl_trust.SSLTrust.apply_backend_ssl_trust`),
and the merged-bundle semantics (system + custom) that keep
public-internet TLS working.
"""

import logging
import os
import ssl
import types
from pathlib import Path

import pytest

from klangk_backend import ssl_trust
from _helpers import make_settings


def _settings(env: dict):
    """Build settings carrying the test's env overrides."""
    return make_settings(env)


def _trust(s) -> ssl_trust.SSLTrust:
    """Build an SSLTrust owning the given settings (#1567).

    SSLTrust only reads ``app_state.state.settings``, so a bare namespace is enough.
    """
    return ssl_trust.SSLTrust(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=s))
    )


@pytest.fixture(autouse=True)
def _restore_trust_env(monkeypatch):
    """Snapshot/restore the trust env vars around each test.

    ``apply_backend_ssl_trust`` mutates ``os.environ`` directly (it must, so
    OpenSSL/httpx/smtplib pick the bundle up), so a manual restore keeps tests
    isolated and avoids leaking a host-path bundle into later tests.
    """
    snapshot = {k: os.environ.get(k) for k in ssl_trust.SSL_TRUST_VARS}
    yield
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestSslCertDir:
    def test_unset_returns_none(self):
        assert _trust(_settings({})).ssl_cert_dir() is None

    def test_missing_dir_returns_none(self, tmp_path):
        s = _settings({"KLANGK_SSL_CERT_DIR": str(tmp_path / "nope")})
        assert _trust(s).ssl_cert_dir() is None

    def test_empty_dir_returns_none(self, tmp_path):
        (tmp_path / "readme.txt").write_text("no certs")
        s = _settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert _trust(s).ssl_cert_dir() is None

    def test_pem_and_crt_detected(self, tmp_path):
        (tmp_path / "a.pem").write_text("CERTA")
        (tmp_path / "b.CRT").write_text("CERTB")
        s = _settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert _trust(s).ssl_cert_dir() == str(tmp_path.resolve())

    def test_falls_back_to_customize_dir_certs(self, tmp_path):
        # When KLANGK_SSL_CERT_DIR is unset but <customize_dir>/certs
        # exists with cert files, it is used.  See #1360.
        custom = tmp_path / "cust"
        certs = custom / "certs"
        certs.mkdir(parents=True)
        (certs / "ca.pem").write_text("CERT")
        s = _settings({"KLANGK_CUSTOMIZE_DIR": str(custom)})
        assert _trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_customize_dir_certs_ignored_when_empty(self, tmp_path):
        # An empty certs/ subdir is treated the same as missing.
        custom = tmp_path / "cust"
        certs = custom / "certs"
        certs.mkdir(parents=True)
        s = _settings({"KLANGK_CUSTOMIZE_DIR": str(custom)})
        assert _trust(s).ssl_cert_dir() is None


class TestSslEnvVars:
    def test_empty_without_dir(self):
        assert ssl_trust.ssl_env_vars(None) == []

    def test_all_four_toolchain_vars(self):
        vars_ = ssl_trust.ssl_env_vars("/some/dir")
        assert vars_ == [
            "SSL_CERT_FILE=/tmp/klangk/ca-bundle.crt",
            "REQUESTS_CA_BUNDLE=/tmp/klangk/ca-bundle.crt",
            "CURL_CA_BUNDLE=/tmp/klangk/ca-bundle.crt",
            "NODE_EXTRA_CA_CERTS=/tmp/klangk/ca-bundle.crt",
        ]


class TestApplyBackendSslTrust:
    def test_noop_when_unset(self):
        assert _trust(_settings({})).apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ

    def test_noop_when_dir_has_no_certs(self, tmp_path):
        s = _settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert _trust(s).apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ

    def test_applies_merged_bundle_and_env_vars(self, monkeypatch, tmp_path):
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "corp-ca.pem").write_text("FAKE-CORP-CA\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")
        data_dir = tmp_path / "data"
        s = _settings(
            {
                "KLANGK_SSL_CERT_DIR": str(cert_dir),
                "KLANGK_DATA_DIR": str(data_dir),
            }
        )

        bundle = _trust(s).apply_backend_ssl_trust()

        assert bundle is not None
        assert os.path.isfile(bundle)
        contents = Path(bundle).read_text()
        # System bundle first (preserves public-internet trust), then custom.
        assert contents == "FAKE-SYSTEM-CA\nFAKE-CORP-CA\n"
        # All toolchain vars point at the merged bundle.
        for k in ssl_trust.SSL_TRUST_VARS:
            assert os.environ[k] == bundle

    def test_bundle_is_merged_not_custom_only(self, monkeypatch, tmp_path):
        """The bundle must include system CAs so public-internet TLS still works
        (SSL_CERT_FILE/REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE REPLACE the store)."""
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "corp-ca.crt").write_text("CORP\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("SYSTEM-MARKER\n")
        s = _settings(
            {
                "KLANGK_SSL_CERT_DIR": str(cert_dir),
                "KLANGK_DATA_DIR": str(tmp_path / "data"),
            }
        )

        _trust(s).apply_backend_ssl_trust()

        bundle = os.environ["SSL_CERT_FILE"]
        contents = Path(bundle).read_text()
        assert "SYSTEM-MARKER" in contents  # public-internet CAs preserved
        assert "CORP" in contents  # custom CA present

    def test_idempotent_no_bundle_growth(self, monkeypatch, tmp_path):
        """Re-applying (e.g. lifespan re-entry) must not duplicate contents.

        Guards against a self-reference: once SSL_CERT_FILE is set, a naive
        system-bundle lookup could read our own merged bundle and re-append
        the custom certs on each call, growing unbounded.
        """
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "corp-ca.pem").write_text("CORP\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("SYSTEM\n")
        s = _settings(
            {
                "KLANGK_SSL_CERT_DIR": str(cert_dir),
                "KLANGK_DATA_DIR": str(tmp_path / "data"),
            }
        )

        trust = _trust(s)
        trust.apply_backend_ssl_trust()
        first = os.environ["SSL_CERT_FILE"]
        size_after_first = os.path.getsize(first)
        contents_after_first = Path(first).read_text()

        trust.apply_backend_ssl_trust()
        second = os.environ["SSL_CERT_FILE"]
        assert second == first
        assert os.path.getsize(second) == size_after_first
        assert (
            Path(second).read_text() == contents_after_first
        )  # no duplication

    def test_custom_cert_missing_system_bundle_warns(
        self, monkeypatch, tmp_path, caplog
    ):
        """When no system bundle is available we warn (public-internet risk)."""
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "corp-ca.pem").write_text("CORP\n")
        monkeypatch.setattr(ssl_trust, "system_ca_bundle", lambda **kw: None)
        s = _settings(
            {
                "KLANGK_SSL_CERT_DIR": str(cert_dir),
                "KLANGK_DATA_DIR": str(tmp_path / "data"),
            }
        )

        with caplog.at_level(logging.WARNING):
            _trust(s).apply_backend_ssl_trust()
        # Still applied (custom certs present), but warned about system loss.
        assert os.environ["SSL_CERT_FILE"]
        assert any("system bundle" in r.message for r in caplog.records)


class TestSystemCaBundle:
    """system_ca_bundle() resolution, fallback chain, and self-reference guard."""

    def test_real_host_bundle_resolves(self):
        # Real call: openssl_cafile resolves to a file on the test host (or
        # certifi fallback). Never raises; result is an existing file or None.
        got = ssl_trust.system_ca_bundle()
        assert got is None or os.path.isfile(got)

    @staticmethod
    def _dvp(cafile, openssl_cafile):
        return ssl.DefaultVerifyPaths(
            cafile=cafile,
            capath=None,
            openssl_cafile_env="SSL_CERT_FILE",
            openssl_cafile=openssl_cafile,
            openssl_capath_env="SSL_CERT_DIR",
            openssl_capath="",
        )

    def test_distinct_cafile_candidate_used(self, monkeypatch, tmp_path):
        sys_pem = tmp_path / "sys.pem"
        sys_pem.write_text("SYS")
        # openssl_cafile empty; distinct cafile present and readable.
        monkeypatch.setattr(
            ssl_trust.ssl,
            "get_default_verify_paths",
            lambda: self._dvp(str(sys_pem), ""),
        )
        assert ssl_trust.system_ca_bundle() == str(sys_pem)

    def test_certifi_fallback_when_no_candidates(self, monkeypatch):
        import certifi

        monkeypatch.setattr(
            ssl_trust.ssl,
            "get_default_verify_paths",
            lambda: self._dvp("", ""),
        )
        assert ssl_trust.system_ca_bundle() == certifi.where()

    def test_none_when_no_candidates_and_certifi_missing_file(
        self, monkeypatch
    ):
        # No default-path candidates, and certifi.where() points at a file
        # that doesn't exist -> no resolvable system bundle.
        monkeypatch.setattr(
            ssl_trust.ssl,
            "get_default_verify_paths",
            lambda: self._dvp("", ""),
        )
        monkeypatch.setattr(
            ssl_trust,
            "certifi",
            types.SimpleNamespace(where=lambda: "/no/such/cacert.pem"),
        )
        assert ssl_trust.system_ca_bundle() is None

    def test_skips_self_reference(self, monkeypatch, tmp_path):
        me = tmp_path / "me.crt"
        me.write_text("ME")
        # The only candidate equals self_bundle -> skipped -> certifi fallback.
        monkeypatch.setattr(
            ssl_trust.ssl,
            "get_default_verify_paths",
            lambda: self._dvp(str(me), str(me)),
        )
        assert ssl_trust.system_ca_bundle(self_bundle=str(me)) != str(me)


class TestInternalsAndErrorBranches:
    """Cover defensive error branches for full line coverage."""

    def test_iter_cert_files_oserror(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            ssl_trust.os,
            "listdir",
            lambda p: (_ for _ in ()).throw(OSError("denied")),
        )
        assert list(ssl_trust.iter_cert_files(str(tmp_path))) == []

    def test_write_skips_unreadable_system_bundle(self, monkeypatch, tmp_path):
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "corp.pem").write_text("CORP\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: "/no/such/sys.pem",
        )
        out = tmp_path / "bundle.crt"
        ok = ssl_trust.write_merged_bundle(str(out), str(cert_dir))
        assert ok is True
        assert Path(out).read_text() == "CORP\n"  # system skipped, custom kept

    def test_write_empty_when_cert_unreadable(self, monkeypatch, tmp_path):
        out = tmp_path / "bundle.crt"
        monkeypatch.setattr(
            ssl_trust, "iter_cert_files", lambda d: ["/no/such/cert.pem"]
        )
        monkeypatch.setattr(
            ssl_trust, "system_ca_bundle", lambda self_bundle=None: None
        )
        assert ssl_trust.write_merged_bundle(str(out), str(tmp_path)) is False

    def test_apply_returns_none_when_makedirs_fails(
        self, monkeypatch, tmp_path
    ):
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        (cert_dir / "c.pem").write_text("C")
        monkeypatch.setattr(
            ssl_trust.os,
            "makedirs",
            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
        )
        assert (
            _trust(
                _settings(
                    {
                        "KLANGK_SSL_CERT_DIR": str(cert_dir),
                        "KLANGK_DATA_DIR": str(tmp_path / "data"),
                    }
                )
            ).apply_backend_ssl_trust()
            is None
        )

    def test_apply_warns_on_empty_bundle(self, monkeypatch, tmp_path, caplog):
        cert_dir = tmp_path / "ssl"
        cert_dir.mkdir()
        monkeypatch.setattr(
            ssl_trust, "system_ca_bundle", lambda self_bundle=None: None
        )
        monkeypatch.setattr(
            ssl_trust, "iter_cert_files", lambda d: ["/nope/cert.pem"]
        )
        with caplog.at_level(logging.WARNING):
            assert (
                _trust(
                    _settings(
                        {
                            "KLANGK_SSL_CERT_DIR": str(cert_dir),
                            "KLANGK_DATA_DIR": str(tmp_path / "data"),
                        }
                    )
                ).apply_backend_ssl_trust()
                is None
            )
        assert any("empty" in r.message for r in caplog.records)
