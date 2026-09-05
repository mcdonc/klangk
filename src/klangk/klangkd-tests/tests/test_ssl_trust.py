"""Tests for runtime SSL/CA certificate trust (#1181).

Covers the shared resolver (:meth:`ssl_trust.SSLTrust.ssl_cert_dir`), the
backend-process trust path (:meth:`ssl_trust.SSLTrust.apply_backend_ssl_trust`),
the merged-bundle semantics (system + custom) that keep
public-internet TLS working, and the ``KLANGKD_TRUSTED_CA_DIR`` approved-CA
baseline (#3198).
"""

import datetime
import logging
import os
import ssl
import types
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from klangk import ssl_trust
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


def _certs_dir(tmp_path):
    """Create ``<tmp_path>/custom/certs/`` and return ``(certs, customize_dir)``.

    Custom certs now live solely under ``<customize_dir>/certs/`` (#1523), so
    tests configure them via ``KLANGKD_CUSTOMIZE_DIR`` rather than the removed
    ``KLANGK_SSL_CERT_DIR``.
    """
    customize = tmp_path / "custom"
    certs = customize / "certs"
    certs.mkdir(parents=True)
    return certs, customize


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

    def test_missing_certs_dir_returns_none(self, tmp_path):
        # customize_dir set, but no certs/ subdir -> None.
        s = _settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert _trust(s).ssl_cert_dir() is None

    def test_customize_dir_certs_detected(self, tmp_path):
        custom = tmp_path / "cust"
        certs = custom / "certs"
        certs.mkdir(parents=True)
        (certs / "a.pem").write_text("CERTA")
        (certs / "b.CRT").write_text("CERTB")
        s = _settings({"KLANGKD_CUSTOMIZE_DIR": str(custom)})
        assert _trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_customize_dir_certs_ignored_when_empty(self, tmp_path):
        # An empty certs/ subdir is treated the same as missing.
        custom = tmp_path / "cust"
        (custom / "certs").mkdir(parents=True)
        s = _settings({"KLANGKD_CUSTOMIZE_DIR": str(custom)})
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
        (tmp_path / "certs").mkdir()  # exists, but empty
        s = _settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert _trust(s).apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ

    def test_applies_merged_bundle_and_env_vars(self, monkeypatch, tmp_path):
        cert_dir, customize = _certs_dir(tmp_path)
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
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_DATA_DIR": str(data_dir),
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
        cert_dir, customize = _certs_dir(tmp_path)
        (cert_dir / "corp-ca.crt").write_text("CORP\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("SYSTEM-MARKER\n")
        s = _settings(
            {
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
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
        cert_dir, customize = _certs_dir(tmp_path)
        (cert_dir / "corp-ca.pem").write_text("CORP\n")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("SYSTEM\n")
        s = _settings(
            {
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
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
        cert_dir, customize = _certs_dir(tmp_path)
        (cert_dir / "corp-ca.pem").write_text("CORP\n")
        monkeypatch.setattr(ssl_trust, "system_ca_bundle", lambda **kw: None)
        s = _settings(
            {
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
            }
        )

        with caplog.at_level(logging.WARNING):
            _trust(s).apply_backend_ssl_trust()
        # Still applied (custom certs present), but warned about system loss.
        assert os.environ["SSL_CERT_FILE"]
        assert any("system bundle" in r.message for r in caplog.records)


def _make_ca_cert(cn: str) -> x509.Certificate:
    """A minimal self-signed CA cert (CA basic constraints, EC key)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )


def _write_ca_pem(path, cn: str) -> str:
    """Write a fresh CA cert as PEM; returns its PEM text."""
    pem = _make_ca_cert(cn).public_bytes(serialization.Encoding.PEM)
    Path(path).write_bytes(pem)
    return pem.decode()


class TestTrustedCaAllowlist:
    """``KLANGKD_TRUSTED_CA_DIR`` — approved-CA baseline (#3198)."""

    @staticmethod
    def _settings_with_baseline(tmp_path, **env):
        """Settings with a trusted CA dir + customize dir wired up."""
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        customize = tmp_path / "custom"
        s = _settings(
            {
                "KLANGKD_TRUSTED_CA_DIR": str(baseline),
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
                "KLANGKD_DATA_DIR": str(tmp_path / "data"),
                **env,
            }
        )
        return s, baseline, customize

    @staticmethod
    def _staged_pems(settings) -> list[str]:
        """The staged approved-CA PEM contents, sorted (empty when absent)."""
        stage = ssl_trust.SSLTrust(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=settings)
            )
        ).ssl_cert_dir()
        if stage is None:
            return []
        files = sorted(Path(stage).glob("ca-*.pem"))
        return [f.read_text() for f in files]

    def test_baseline_staged_as_trust_source(self, tmp_path):
        # Baseline holds a CA; customize certs dir absent -> the staged copy
        # of the baseline (canonical PEM, under <state>/ssl/approved) is the
        # trust source both scopes consume.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        pem = _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        got = _trust(s).ssl_cert_dir()
        assert got == os.path.join(str(tmp_path / "state"), "ssl", "approved")
        assert sorted(os.listdir(got)) == ["ca-000.pem"]
        assert Path(got, "ca-000.pem").read_text() == pem

    def test_baseline_with_empty_customize_dir(self, tmp_path, caplog):
        # Empty customize certs dir: no audit output, baseline still trusted.
        s, baseline, customize = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        (customize / "certs").mkdir(parents=True)
        with caplog.at_level(logging.INFO):
            assert _trust(s).ssl_cert_dir() is not None
        assert not caplog.records

    def test_approved_custom_cert_logged(self, tmp_path, caplog):
        # The same CA in the customize dir is fingerprint-approved (debug;
        # per-start noise control keeps warnings for refusals only).
        s, baseline, customize = self._settings_with_baseline(tmp_path)
        pem = _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        certs = customize / "certs"
        certs.mkdir(parents=True)
        (certs / "same-ca.pem").write_text(pem)
        with caplog.at_level(logging.DEBUG):
            assert _trust(s).ssl_cert_dir() is not None
        assert any(
            "approved" in r.message and "DoD Root CA 3" in r.message
            for r in caplog.records
            if r.levelno == logging.DEBUG
        )

    def test_nonapproved_custom_cert_refused(self, tmp_path, caplog):
        # A foreign CA in the customize dir is refused with a warning
        # naming subject/issuer — and is absent from the staged trust set.
        s, baseline, customize = self._settings_with_baseline(tmp_path)
        baseline_pem = _write_ca_pem(
            baseline / "dod-root.pem", "DoD Root CA 3"
        )
        certs = customize / "certs"
        certs.mkdir(parents=True)
        _write_ca_pem(certs / "shadow-ca.pem", "Shadow CA")
        with caplog.at_level(logging.WARNING):
            assert _trust(s).ssl_cert_dir() is not None
        assert any(
            "Refusing non-approved CA" in r.message
            and "Shadow CA" in r.message
            and str(baseline) in r.message
            for r in caplog.records
        )
        pems = self._staged_pems(s)
        assert pems == [baseline_pem]  # foreign CA never staged

    def test_unparseable_custom_cert_refused(self, tmp_path, caplog):
        # Garbage in the customize dir cannot be verified -> refused.
        s, baseline, customize = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        certs = customize / "certs"
        certs.mkdir(parents=True)
        (certs / "garbage.pem").write_text("not a cert")
        with caplog.at_level(logging.WARNING):
            assert _trust(s).ssl_cert_dir() is not None
        assert any(
            "unparseable" in r.message and "garbage.pem" in r.message
            for r in caplog.records
        )

    def test_missing_baseline_fails_closed(self, tmp_path, caplog):
        # Baseline dir does not exist -> nothing trusted, error logged.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        baseline.rmdir()
        with caplog.at_level(logging.ERROR):
            assert _trust(s).ssl_cert_dir() is None
        assert any("fail closed" in r.message for r in caplog.records)

    def test_empty_baseline_fails_closed(self, tmp_path, caplog):
        # Baseline exists but holds no certs -> nothing trusted, error logged.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        (baseline / "notes.txt").write_text("no certs here")
        with caplog.at_level(logging.ERROR):
            assert _trust(s).ssl_cert_dir() is None
        assert any("fail closed" in r.message for r in caplog.records)

    def test_unparseable_baseline_file_excluded_entirely(
        self, tmp_path, caplog
    ):
        # One good + one garbage baseline file: the garbage is excluded from
        # the fingerprint set AND from the staged dir (its raw bytes never
        # reach a trust bundle, even if a lenient parser would accept them).
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        pem = _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        (baseline / "broken.crt").write_text("garbage")
        with caplog.at_level(logging.ERROR):
            assert _trust(s).ssl_cert_dir() is not None
        assert any(
            "unparseable" in r.message and "broken.crt" in r.message
            for r in caplog.records
        )
        assert self._staged_pems(s) == [pem]

    def test_duplicate_baseline_certs_deduped(self, tmp_path):
        # The same CA in two baseline files is staged once (fingerprint
        # identity, not filename identity).
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        pem = _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        (baseline / "dod-root-copy.pem").write_text(pem)
        assert self._staged_pems(s) == [pem]

    def test_multi_cert_pem_baseline(self, tmp_path):
        # A single baseline .pem holding two certs: both are staged
        # (matching is per certificate, not per file).
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        cert_a = _make_ca_cert("DoD Root CA A")
        cert_b = _make_ca_cert("DoD Root CA B")
        blob = cert_a.public_bytes(
            serialization.Encoding.PEM
        ) + cert_b.public_bytes(serialization.Encoding.PEM)
        (baseline / "bundle.pem").write_bytes(blob)
        staged = self._staged_pems(s)
        assert sorted(staged) == sorted(
            [
                cert_a.public_bytes(serialization.Encoding.PEM).decode(),
                cert_b.public_bytes(serialization.Encoding.PEM).decode(),
            ]
        )

    def test_der_baseline_cert_parsed(self, tmp_path):
        # DER-encoded .crt in the baseline is parsed (PEM-first fallback) and
        # staged as canonical PEM.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        cert = _make_ca_cert("DoD DER CA")
        (baseline / "dod-der.crt").write_bytes(
            cert.public_bytes(serialization.Encoding.DER)
        )
        assert self._staged_pems(s) == [
            cert.public_bytes(serialization.Encoding.PEM).decode()
        ]

    def test_staging_shrinks_with_baseline(self, tmp_path):
        # Re-resolving after the baseline shrinks removes the stale staged
        # file — the staged dir always mirrors exactly the current baseline.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "a.pem", "Approved CA A")
        _write_ca_pem(baseline / "b.pem", "Approved CA B")
        assert len(self._staged_pems(s)) == 2
        (baseline / "b.pem").unlink()
        assert len(self._staged_pems(s)) == 1

    def test_staging_failure_fails_closed(self, monkeypatch, tmp_path, caplog):
        # A staging-dir error (unwritable state dir) must not degrade into
        # trusting the raw baseline dir: fail closed with an error log.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")

        def boom(*a, **k):
            raise OSError("read-only state dir")

        monkeypatch.setattr(ssl_trust.os, "makedirs", boom)
        with caplog.at_level(logging.ERROR):
            assert _trust(s).ssl_cert_dir() is None
        assert any("failing closed" in r.message for r in caplog.records)

    def test_backend_bundle_excludes_refused_cert(self, monkeypatch, tmp_path):
        # End-to-end backend trust: merged bundle = system + staged baseline
        # only; the refused customize-dir CA never reaches it.
        s, baseline, customize = self._settings_with_baseline(tmp_path)
        baseline_pem = _write_ca_pem(
            baseline / "dod-root.pem", "DoD Root CA 3"
        )
        certs = customize / "certs"
        certs.mkdir(parents=True)
        foreign_pem = _write_ca_pem(certs / "shadow-ca.pem", "Shadow CA")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")

        bundle = _trust(s).apply_backend_ssl_trust()

        assert bundle is not None
        contents = Path(bundle).read_text()
        assert "FAKE-SYSTEM-CA\n" in contents
        assert baseline_pem in contents  # approved baseline trusted
        assert foreign_pem not in contents  # refused CA excluded
        for k in ssl_trust.SSL_TRUST_VARS:
            assert os.environ[k] == bundle

    def test_reload_to_no_trust_revokes_stale_trust(
        self, monkeypatch, tmp_path, caplog
    ):
        # Fail-closed across reloads: trust applied at boot, then the
        # baseline is emptied and apply runs again (the SIGHUP path) ->
        # the trust vars we set are cleared, the stale bundle and the
        # staged cert dir are removed.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")
        trust = _trust(s)
        bundle = trust.apply_backend_ssl_trust()
        assert bundle is not None and os.path.isfile(bundle)
        staged = os.path.join(str(tmp_path / "state"), "ssl", "approved")
        assert os.path.isdir(staged)

        (baseline / "dod-root.pem").unlink()  # operator empties the baseline
        with caplog.at_level(logging.WARNING):
            assert trust.apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ  # revoked, not silently stale
        assert not os.path.exists(bundle)  # stale bundle deleted
        assert not os.path.exists(staged)  # staged residue cleaned
        assert any("revoked" in r.message for r in caplog.records)

        # Idempotent: a second no-trust apply is a quiet no-op.
        assert trust.apply_backend_ssl_trust() is None

    def test_build_failure_revokes_stale_trust(self, monkeypatch, tmp_path):
        # A trust source that resolves but fails to build a bundle (empty
        # result) must also revoke — never leave the vars pointing at a
        # stale bundle (#3198 review).
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")
        trust = _trust(s)
        bundle = trust.apply_backend_ssl_trust()
        assert bundle is not None and os.path.isfile(bundle)

        monkeypatch.setattr(ssl_trust, "write_merged_bundle", lambda *a: False)
        assert trust.apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ  # revoked, not silently stale
        assert not os.path.exists(bundle)

    def test_bundle_write_oserror_fails_closed(
        self, monkeypatch, tmp_path, caplog
    ):
        # An OSError while writing the bundle (disk full, EROFS) is caught
        # (never propagates out of apply) and lands in the fail-closed path.
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(ssl_trust, "write_merged_bundle", boom)
        with caplog.at_level(logging.ERROR):
            assert _trust(s).apply_backend_ssl_trust() is None
        assert any("disk full" in r.message for r in caplog.records)
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ

    def test_empty_baseline_env_string_behaves_as_unset(self, tmp_path):
        # KLANGKD_TRUSTED_CA_DIR="" is falsy -> no restriction; the raw
        # customize certs dir remains the trust source (documented).
        certs, customize = _certs_dir(tmp_path)
        (certs / "corp-ca.pem").write_text("CORP\n")
        s = _settings(
            {
                "KLANGKD_TRUSTED_CA_DIR": "",
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
            }
        )
        assert _trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_revoke_after_state_dir_change(self, monkeypatch, tmp_path):
        # #3198 review r3: revocation must target the APPLIED bundle path,
        # not the one the reloaded settings derive — a SIGHUP that changes
        # state_dir while removing the trust source must still clear the
        # vars (the remembered path, not the recomputed one).
        s1, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")
        trust = _trust(s1)
        bundle = trust.apply_backend_ssl_trust()
        assert bundle is not None and os.path.isfile(bundle)

        (baseline / "dod-root.pem").unlink()
        s2 = _settings(
            {
                "KLANGKD_TRUSTED_CA_DIR": str(baseline),
                "KLANGKD_CUSTOMIZE_DIR": str(tmp_path / "custom"),
                "KLANGKD_STATE_DIR": str(tmp_path / "state2"),
                "KLANGKD_DATA_DIR": str(tmp_path / "data2"),
            }
        )
        trust.app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=s2)
        )  # the SIGHUP settings swap
        assert trust.apply_backend_ssl_trust() is None
        for k in ssl_trust.SSL_TRUST_VARS:
            assert k not in os.environ  # revoked despite the path change
        assert not os.path.exists(bundle)  # old bundle removed

    def test_unreadable_baseline_fails_closed(
        self, monkeypatch, tmp_path, caplog
    ):
        # A permission-denied baseline dir gets a specific error (not the
        # generic "no usable certificates" that hides the EACCES).
        s, baseline, _ = self._settings_with_baseline(tmp_path)
        _write_ca_pem(baseline / "dod-root.pem", "DoD Root CA 3")

        def boom(path):
            raise OSError("permission denied")

        monkeypatch.setattr(ssl_trust.os, "listdir", boom)
        with caplog.at_level(logging.ERROR):
            assert _trust(s).ssl_cert_dir() is None
        assert any(
            "cannot read" in r.message and "permission denied" in r.message
            for r in caplog.records
        )

    def test_der_custom_cert_without_baseline_does_not_crash(
        self, monkeypatch, tmp_path
    ):
        # Pre-existing crash pinned (#3198 review r3): a binary DER .crt in
        # the customize certs dir (no baseline) used to raise
        # UnicodeDecodeError from the text-mode bundle append at startup.
        certs, customize = _certs_dir(tmp_path)
        der = _make_ca_cert("DER Corp CA").public_bytes(
            serialization.Encoding.DER
        )
        (certs / "der-ca.crt").write_bytes(der)
        monkeypatch.setattr(
            ssl_trust,
            "system_ca_bundle",
            lambda self_bundle=None: str(tmp_path / "sys.pem"),
        )
        (tmp_path / "sys.pem").write_text("FAKE-SYSTEM-CA\n")
        s = _settings(
            {
                "KLANGKD_CUSTOMIZE_DIR": str(customize),
                "KLANGKD_STATE_DIR": str(tmp_path / "state"),
            }
        )

        bundle = _trust(s).apply_backend_ssl_trust()

        assert bundle is not None
        assert der in Path(bundle).read_bytes()  # bytes copied verbatim


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
        cert_dir, customize = _certs_dir(tmp_path)
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
                        "KLANGKD_CUSTOMIZE_DIR": str(customize),
                        "KLANGKD_DATA_DIR": str(tmp_path / "data"),
                    }
                )
            ).apply_backend_ssl_trust()
            is None
        )

    def test_apply_warns_on_empty_bundle(self, monkeypatch, tmp_path, caplog):
        cert_dir, customize = _certs_dir(tmp_path)
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
                            "KLANGKD_CUSTOMIZE_DIR": str(customize),
                            "KLANGKD_DATA_DIR": str(tmp_path / "data"),
                        }
                    )
                ).apply_backend_ssl_trust()
                is None
            )
        assert any("empty" in r.message for r in caplog.records)
