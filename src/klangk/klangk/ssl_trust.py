"""Runtime SSL/CA certificate trust, without an image rebuild (#1181).

A deployer drops ``.pem``/``.crt`` CA certificates into
``<KLANGKD_CUSTOMIZE_DIR>/certs`` and both trust scopes consume them at runtime:

* **Workspace containers** — :mod:`container` mounts the directory read-only at
  :data:`SSL_MOUNT_DEST` and emits the :data:`SSL_TRUST_VARS` env vars pointing
  at :data:`SSL_BUNDLE_DEST`, the in-container bundle the entrypoint builds
  from the mounted certs plus the container's system bundle.

* **Backend process** — :meth:`SSLTrust.apply_backend_ssl_trust` builds a host-side
  bundle and sets the trust vars in :data:`os.environ` so the backend's own
  outbound TLS (OIDC IdP, SMTP relay, LLM-proxy upstream) honors the private
  CAs.  Called once at startup (:func:`klangk.main.lifespan`).

**Both bundles are *merged* (system CAs + custom certs).** The
``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` vars *replace*
the default trust store rather than augment it, so a custom-only bundle would
break public-internet TLS (``npm``/``pip``/``git``, public OIDC, Gmail SMTP).
``NODE_EXTRA_CA_CERTS`` is additive, but pointing it at the merged bundle is
harmless (Node de-duplicates).

**CA allowlist (#3198).** ``KLANGKD_TRUSTED_CA_DIR`` points at an
operator-managed *approved CA baseline* (e.g. the DoD-approved CA set).
When set, only baseline CAs are trusted: :meth:`SSLTrust.ssl_cert_dir`
returns a **staged** directory (``<state_dir>/ssl/approved``) holding one
canonical PEM per unique parseable baseline cert — so both trust scopes
consume exactly the certs whose SHA-256 fingerprints were vetted, and a
file the strict parser rejects never reaches a bundle even if a lenient
consumer might have accepted its raw bytes. Every cert dropped into
``<KLANGKD_CUSTOMIZE_DIR>/certs`` is audited against the baseline — a cert
whose fingerprint is in the baseline logs at debug, anything else (or an
unparseable file) is **refused** with a warning naming its subject/issuer.
A missing or cert-less baseline fails *closed*: no custom CAs are trusted
at all, and trust applied earlier in the process is revoked on reload.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import ssl

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization


logger = logging.getLogger(__name__)

# File extensions that count as CA certificates (matched case-insensitively).
SSL_CERT_EXTS = (".pem", ".crt")
# In-container mount point for the read-only deployer cert directory.
SSL_MOUNT_DEST = "/opt/klangk/ssl"
# In-container CA bundle path.  Built by the container entrypoint at startup
# from the mounted certs plus the container's system bundle, on the writable
# /tmp tmpfs (the entrypoint runs as non-root UID 1000).
SSL_BUNDLE_DEST = "/tmp/klangk/ca-bundle.crt"
# Toolchains whose trust we redirect at the merged bundle.  SSL_CERT_FILE is
# OpenSSL / the stdlib ``ssl`` module / smtplib; REQUESTS_CA_BUNDLE is
# ``requests``; CURL_CA_BUNDLE is curl; NODE_EXTRA_CA_CERTS is Node.
SSL_TRUST_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)


def parse_cert_bytes(data: bytes) -> list[x509.Certificate]:
    """Parse cert material: PEM (any number of certs), else one DER cert.

    Raises ``ValueError`` when the bytes are neither — callers treat that as
    an unparseable file.
    """
    try:
        return x509.load_pem_x509_certificates(data)
    except ValueError:
        return [x509.load_der_x509_certificate(data)]


def load_dir_certs(
    cert_dir: str,
) -> tuple[list[x509.Certificate], list[tuple[str, str]]]:
    """Parse every ``.pem``/``.crt`` file in ``cert_dir``.

    Returns ``(certs, errors)`` where *errors* is one ``(path, reason)`` per
    unreadable or unparseable file (those contribute no certs). Never raises.
    """
    certs: list[x509.Certificate] = []
    errors: list[tuple[str, str]] = []
    for path in iter_cert_files(cert_dir):
        try:
            with open(path, "rb") as f:
                certs.extend(parse_cert_bytes(f.read()))
        except (OSError, ValueError) as exc:
            errors.append((path, str(exc)))
    return certs, errors


def cert_fingerprint(cert: x509.Certificate) -> bytes:
    """Stable identity of a cert for baseline matching: SHA-256 of its DER."""
    return cert.fingerprint(hashes.SHA256())


def cert_label(cert: x509.Certificate) -> str:
    """Human-facing identity of a cert for audit logs."""
    return (
        f"subject={cert.subject.rfc4514_string()} "
        f"issuer={cert.issuer.rfc4514_string()}"
    )


def canonical_pem(cert: x509.Certificate) -> bytes:
    """The PEM encoding of exactly the DER we fingerprinted."""
    return cert.public_bytes(serialization.Encoding.PEM)


def remove_stale_certs(stage_dir: str, keep: set[str]) -> None:
    """Delete staged cert files whose names are not in *keep* (best effort).

    Keeps a re-staged directory free of files from a previous, larger
    baseline. Never raises — an unlink failure leaves at most a stale
    previously-approved cert, not a new one.
    """
    for path in iter_cert_files(stage_dir):
        if os.path.basename(path) not in keep:
            with contextlib.suppress(OSError):
                os.unlink(path)


def baseline_read_error(cert_dir: str) -> str | None:
    """Why *cert_dir* cannot be read (missing / unreadable), else ``None``.

    Lets the fail-closed log name an EACCES instead of hiding behind the
    generic empty-baseline wording (#3198 review).
    """
    if not os.path.isdir(cert_dir):
        return f"{cert_dir} is not a directory"
    try:
        os.listdir(cert_dir)
    except OSError as exc:
        return f"cannot read {cert_dir}: {exc}"
    return None


def iter_cert_files(ssl_dir: str):
    """Yield absolute paths of ``*.pem``/``*.crt`` files in ``ssl_dir``.

    Sorted for deterministic bundle contents; case-insensitive extension
    match.  Empty when the directory is unreadable or contains no certs.
    """
    try:
        names = sorted(os.listdir(ssl_dir))
    except OSError:
        return
    for name in names:
        if name.lower().endswith(SSL_CERT_EXTS):
            yield os.path.join(ssl_dir, name)


def ssl_env_vars(ssl_dir: str | None) -> list[str]:
    """Container env vars pointing toolchains at the in-container bundle.

    Empty unless a trustable cert dir is configured
    (see :meth:`SSLTrust.ssl_cert_dir`).
    The bundle itself is built by the container entrypoint at startup from the
    mounted certs plus the container's system bundle.
    """
    if not ssl_dir:
        return []
    return [f"{name}={SSL_BUNDLE_DEST}" for name in SSL_TRUST_VARS]


def system_ca_bundle(self_bundle: str | None = None) -> str | None:
    """Best-effort path to the host's default (system) CA bundle.

    The trust env vars *replace* the default store, so a merged bundle must
    include the system CAs to preserve public-internet trust.  Prefers the
    compiled-in OpenSSL default (``openssl_cafile`` — not influenced by the
    ``SSL_CERT_FILE`` env var) over ``cafile`` to avoid a self-reference when
    this function runs after we have already set ``SSL_CERT_FILE`` (idempotent
    re-entry / tests).  Skips any candidate equal to ``self_bundle``.  Falls
    back to ``certifi`` (an httpx dependency).
    """
    dvp = ssl.get_default_verify_paths()
    self_real = os.path.realpath(self_bundle) if self_bundle else None
    cand = _first_existing_ca(_openssl_ca_candidates(dvp), self_real)
    if cand is not None:
        return cand
    return _first_existing_ca([certifi.where()], self_real)


def _openssl_ca_candidates(dvp) -> list[str]:
    """The compiled-in OpenSSL default CA file (preferred — not influenced
    by ``SSL_CERT_FILE``) and the env-derived one, de-duplicated."""
    candidates: list[str] = []
    if dvp.openssl_cafile:
        candidates.append(dvp.openssl_cafile)
    if dvp.cafile and dvp.cafile != dvp.openssl_cafile:
        candidates.append(dvp.cafile)
    return candidates


def _first_existing_ca(candidates: list[str], self_real) -> str | None:
    """The first candidate that exists and is not the bundle being built."""
    for cand in candidates:
        if self_real and os.path.realpath(cand) == self_real:
            continue
        if os.path.isfile(cand):
            return cand
    return None


def _append_bundle_file(out, path: str, warn: str) -> bool:
    """Append one bundle/cert file's raw bytes to *out*; ``False`` (with
    a warning) when unreadable.

    Binary mode: a DER-encoded ``.crt`` in a trust dir would raise
    ``UnicodeDecodeError`` in text mode and crash the apply — any bytes a
    consumer might accept must be copyable (#3198 review).
    """
    try:
        with open(path, "rb") as f:
            out.write(f.read())
        return True
    except OSError as exc:
        logger.warning(warn, path, exc)
        return False


def _append_custom_certs(out, ssl_dir: str) -> int:
    """Append every readable cert in *ssl_dir*; returns how many were
    written (unreadable files are skipped with a warning)."""
    written = 0
    for cert in iter_cert_files(ssl_dir):
        written += _append_bundle_file(
            out, cert, "Skipping unreadable cert %s: %s"
        )
    return written


def write_merged_bundle(bundle_path: str, ssl_dir: str) -> bool:
    """Write system CAs + custom certs to ``bundle_path`` (atomically).

    Builds into ``<bundle_path>.tmp`` and moves it into place only when the
    result is non-empty, so a failed or empty build never truncates a
    previously-good live bundle the trust env vars still point at (#3198
    review). Returns ``True`` if a non-empty bundle was written. System bundle
    is read first so it is never lost; unreadable files are skipped with a
    warning.
    """
    tmp_path = bundle_path + ".tmp"
    written = 0
    with open(tmp_path, "wb") as out:
        sys_bundle = system_ca_bundle(self_bundle=bundle_path)
        if sys_bundle:
            written += _append_bundle_file(
                out, sys_bundle, "Could not read system CA bundle %s: %s"
            )
        written += _append_custom_certs(out, ssl_dir)
    if written > 0 and os.path.getsize(tmp_path) > 0:
        os.replace(tmp_path, bundle_path)
        return True
    with contextlib.suppress(OSError):
        os.unlink(tmp_path)
    return False


def _apply_trust_vars(bundle_path: str) -> None:
    """Point every backend trust env var at the bundle."""
    for name in SSL_TRUST_VARS:
        os.environ[name] = bundle_path


class SSLTrust:
    """Owns the settings-dependent SSL trust surface (#1567).

    The functions that read ``settings`` — the cert-dir resolver
    (:meth:`ssl_cert_dir`, dispatching to :meth:`custom_certs_dir` or the
    ``KLANGKD_TRUSTED_CA_DIR`` baseline path :meth:`approved_certs_dir` /
    :meth:`audit_custom_certs` / :meth:`staged_certs_dir`, #3198) and the
    backend-process trust applier (:meth:`apply_backend_ssl_trust`, with
    its fail-closed counterpart :meth:`revoke_backend_ssl_trust`) — live
    here as methods, reaching the deployer config through
    ``self.app.state.settings`` rather than threading it through every
    call. The pure path/bundle/cert helpers and the module constants stay
    module-level: they take explicit paths or bytes and read no settings.
    """

    def __init__(self, app):
        self.app = app
        # Path of the bundle this process last applied (#3198 review):
        # revocation must target what was actually applied, not whatever the
        # *current* settings derive — a reload that changes ``state_dir``
        # recomputes a different path and would silently skip revoking.
        self._applied_bundle: str | None = None

    def reconfigure(self, app) -> None:
        self.app = app
        self.apply_backend_ssl_trust()

    def ssl_cert_dir(self) -> str | None:
        """Return the directory whose certs should be trusted, else ``None``.

        With ``KLANGKD_TRUSTED_CA_DIR`` set (#3198), that is the staged
        approved-CA directory (built from the operator's baseline after
        validating it and auditing the customize certs dir against it — see
        :meth:`approved_certs_dir`). Otherwise it
        resolves ``<KLANGKD_CUSTOMIZE_DIR>/certs`` — the sole canonical place
        deployers put custom CAs (#1360, #1523) — returning the absolute path
        when the directory exists and contains at least one ``.pem``/``.crt``
        file; ``None`` otherwise (missing, or empty of certs). Never raises —
        a misconfigured path simply disables runtime trust.
        """
        baseline_dir = self.app.state.settings.approved_ca_dir
        if baseline_dir:
            return self.approved_certs_dir(baseline_dir)
        return self.custom_certs_dir()

    def custom_certs_dir(self) -> str | None:
        """The raw ``<KLANGKD_CUSTOMIZE_DIR>/certs`` dir, when it holds certs."""
        customize = self.app.state.settings.customize_dir
        candidate = os.path.join(customize, "certs")
        if not os.path.isdir(candidate):
            return None
        path = os.path.realpath(candidate)
        return path if any(True for _ in iter_cert_files(path)) else None

    def approved_certs_dir(self, baseline_dir: str) -> str | None:
        """Validate the approved CA baseline; stage only its usable certs.

        Returns the path of the staged directory (under
        ``<state_dir>/ssl/approved``, holding one canonical PEM per unique
        parseable baseline cert — the trust source for both scopes), else
        ``None``. Staging — rather than trusting the baseline dir directly —
        guarantees that a file the strict parser rejects is **never** trusted
        (backend bundle and container mounts see only re-encoded certs whose
        DER was fingerprinted), even when a lenient consumer (OpenSSL BER)
        might have accepted the raw bytes (#3198 review).

        A missing/unreadable/cert-less baseline **fails closed**: no custom
        CAs are trusted, with an error log (a dedicated message when the dir
        is missing or unreadable, so an EACCES doesn't hide behind the
        generic empty-baseline wording). Every cert in the customize certs
        dir is checked by SHA-256 fingerprint: approved ones log at debug,
        non-approved or unparseable ones are refused with a warning naming
        subject/issuer.
        """
        read_error = baseline_read_error(baseline_dir)
        if read_error:
            logger.error(
                "KLANGKD_TRUSTED_CA_DIR %s — refusing to trust any custom "
                "CAs (fail closed)",
                read_error,
            )
            return None
        baseline, errors = load_dir_certs(baseline_dir)
        for path, reason in errors:
            logger.error(
                "Ignoring unparseable file %s in KLANGKD_TRUSTED_CA_DIR "
                "(excluded from trust): %s",
                path,
                reason,
            )
        if not baseline:
            logger.error(
                "KLANGKD_TRUSTED_CA_DIR=%s has no usable CA certificates; "
                "refusing to trust any custom CAs (fail closed)",
                baseline_dir,
            )
            return None
        approved = {cert_fingerprint(c): c for c in baseline}
        self.audit_custom_certs(set(approved), baseline_dir)
        return self.staged_certs_dir(approved)

    def _staged_dir(self) -> str:
        """The staged approved-CA dir under ``<state_dir>/ssl/approved``."""
        return os.path.join(
            self.app.state.settings.state_dir, "ssl", "approved"
        )

    def staged_certs_dir(
        self, approved: dict[bytes, x509.Certificate]
    ) -> str | None:
        """Materialize the approved certs under ``<state_dir>/ssl/approved``.

        One ``ca-NNN.pem`` per unique cert (sorted by fingerprint for
        deterministic contents), written via temp-file + rename so a reader
        never sees a partial cert; stale files from a smaller previous
        baseline are removed afterwards. Returns ``None`` (failing closed,
        with an error) when the directory cannot be built.
        """
        stage_dir = self._staged_dir()
        names = {
            f"ca-{i:03d}.pem": approved[fp]
            for i, fp in enumerate(sorted(approved))
        }
        try:
            os.makedirs(stage_dir, exist_ok=True)
            for name, cert in names.items():
                self._write_staged_cert(stage_dir, name, canonical_pem(cert))
            remove_stale_certs(stage_dir, set(names))
        except OSError as exc:
            logger.error(
                "Cannot stage approved CAs in %s: %s — failing closed",
                stage_dir,
                exc,
            )
            return None
        return stage_dir

    @staticmethod
    def _write_staged_cert(stage_dir: str, name: str, pem: bytes) -> None:
        """Atomically place one staged cert file (temp file + rename)."""
        tmp = os.path.join(stage_dir, f".{name}.tmp")
        with open(tmp, "wb") as f:
            f.write(pem)
        os.replace(tmp, os.path.join(stage_dir, name))

    def audit_custom_certs(
        self, approved: set[bytes], baseline_dir: str
    ) -> None:
        """Log the fate of each cert in the customize certs dir.

        Approved (fingerprint in the baseline) → info; non-approved or
        unparseable → warning naming subject/issuer (refused — the cert does
        not reach the merged bundle or any container).
        """
        certs_dir = os.path.join(
            self.app.state.settings.customize_dir, "certs"
        )
        if not os.path.isdir(certs_dir):
            return
        certs, errors = load_dir_certs(certs_dir)
        for path, reason in errors:
            logger.warning(
                "Refusing cert %s in <KLANGKD_CUSTOMIZE_DIR>/certs: "
                "unparseable (%s) — cannot verify it against "
                "KLANGKD_TRUSTED_CA_DIR",
                path,
                reason,
            )
        for cert in certs:
            if cert_fingerprint(cert) in approved:
                logger.debug(
                    "Custom CA approved by KLANGKD_TRUSTED_CA_DIR: %s",
                    cert_label(cert),
                )
            else:
                logger.warning(
                    "Refusing non-approved CA %s: not in "
                    "KLANGKD_TRUSTED_CA_DIR=%s",
                    cert_label(cert),
                    baseline_dir,
                )

    def _bundle_path(self) -> str:
        """The backend merged-bundle path under ``<state_dir>/ssl``."""
        return os.path.join(
            self.app.state.settings.state_dir, "ssl", "ca-bundle.crt"
        )

    def revoke_backend_ssl_trust(self) -> None:
        """Fail-closed counterpart of :meth:`apply_backend_ssl_trust`.

        No-op unless this process previously applied trust (detected by a
        trust var still pointing at the bundle we applied — remembered, so a
        reload that changed ``state_dir`` cannot silently skip revocation).
        Then: unset the trust vars (our own outbound TLS re-reads them per
        connection, so the backend stops honoring the stale bundle
        immediately), delete the bundle file, and remove the staged approved-CA
        dir so no residue is left in state. Already-running child processes
        that inherited the vars keep their inherited environment — deleting
        the bundle makes those fail closed too, which is the documented
        guarantee.
        """
        applied = self._applied_bundle or self._bundle_path()
        if os.environ.get("SSL_CERT_FILE") != applied:
            return
        for name in SSL_TRUST_VARS:
            os.environ.pop(name, None)
        with contextlib.suppress(OSError):
            os.unlink(applied)
        shutil.rmtree(
            self._staged_dir(), ignore_errors=True
        )  # residue only — best effort
        self._applied_bundle = None
        logger.warning(
            "Backend SSL trust revoked: no trustable CA state after "
            "reload; cleared %d env var(s) and removed the stale bundle",
            len(SSL_TRUST_VARS),
        )

    def _build_bundle(self, ssl_dir: str) -> str | None:
        """Write the merged bundle under ``<state_dir>/ssl``; ``None``
        (with a log) when the dir cannot be created or the bundle comes
        out empty."""
        bundle_dir = os.path.join(self.app.state.settings.state_dir, "ssl")
        try:
            os.makedirs(bundle_dir, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Cannot create SSL bundle dir %s: %s", bundle_dir, exc
            )
            return None
        bundle_path = self._bundle_path()
        try:
            ok = write_merged_bundle(bundle_path, ssl_dir)
        except OSError as exc:
            logger.error(
                "Cannot write SSL bundle %s: %s — failing closed",
                bundle_path,
                exc,
            )
            return None
        if not ok:
            logger.warning(
                "SSL bundle %s is empty (no system bundle and no custom certs); "
                "not applying backend trust",
                bundle_path,
            )
            return None
        return bundle_path

    def apply_backend_ssl_trust(self) -> str | None:
        """Make the backend process trust the deployer's custom CAs.

        Builds a merged bundle (system + custom) under ``<data_dir>/ssl`` and sets
        :data:`SSL_TRUST_VARS` in :data:`os.environ`, so the backend's outbound TLS
        (OIDC discovery, SMTP relay, LLM-proxy upstream) honors the private CAs.
        Idempotent and safe to call at startup; a no-op when no cert dir is
        configured.  Refuses to apply trust when the system bundle can't be found
        *and* no cert was written (would risk losing public-internet trust).

        Returns the bundle path, or ``None`` if trust was not applied.
        When no trustable state results but trust was applied earlier in
        this process (no trust source resolves, or the bundle build fails
        — e.g. the approved CA baseline was removed/emptied, or the state
        dir became unwritable — and a SIGHUP reload re-runs this), the
        previously-applied trust is **revoked** — the trust vars we set are
        cleared and the stale bundle removed — so fail-closed holds across
        reloads, not just at boot (#3198).
        """
        ssl_dir = self.ssl_cert_dir()
        if not ssl_dir:
            return self.revoke_backend_ssl_trust()
        bundle_path = self._build_bundle(ssl_dir)
        if bundle_path is None:
            return self.revoke_backend_ssl_trust()
        if not system_ca_bundle(self_bundle=bundle_path):
            logger.warning(
                "Applying backend SSL trust without a system bundle: the trust "
                "vars replace the default store, so public-internet TLS endpoints "
                "may fail. Provide a system CA bundle, or remove the custom certs "
                "from <KLANGKD_CUSTOMIZE_DIR>/certs/ to skip backend trust."
            )
        _apply_trust_vars(bundle_path)
        self._applied_bundle = bundle_path
        logger.info(
            "Backend SSL trust applied: %s -> %d env var(s) (custom certs from %s)",
            bundle_path,
            len(SSL_TRUST_VARS),
            ssl_dir,
        )
        return bundle_path
