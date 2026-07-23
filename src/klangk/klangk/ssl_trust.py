"""Runtime SSL/CA certificate trust, without an image rebuild (#1181).

A deployer drops ``.pem``/``.crt`` CA certificates into ``KLANGKD_SSL_CERT_DIR``
and both trust scopes consume them at runtime:

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
"""

from __future__ import annotations

import logging
import os
import ssl

import certifi


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
    candidates: list[str] = []
    if dvp.openssl_cafile:
        candidates.append(dvp.openssl_cafile)
    if dvp.cafile and dvp.cafile != dvp.openssl_cafile:
        candidates.append(dvp.cafile)
    self_real = os.path.realpath(self_bundle) if self_bundle else None
    for cand in candidates:
        if self_real and os.path.realpath(cand) == self_real:
            continue
        if os.path.isfile(cand):
            return cand
    where = certifi.where()
    if where and (not self_real or os.path.realpath(where) != self_real):
        if os.path.isfile(where):
            return where
    return None


def write_merged_bundle(bundle_path: str, ssl_dir: str) -> bool:
    """Write system CAs + custom certs to ``bundle_path``.

    Returns ``True`` if a non-empty bundle was written.  System bundle is read
    first so it is never lost; unreadable files are skipped with a warning.
    """
    written = 0
    with open(bundle_path, "w") as out:
        sys_bundle = system_ca_bundle(self_bundle=bundle_path)
        if sys_bundle:
            try:
                with open(sys_bundle) as f:
                    out.write(f.read())
                written += 1
            except OSError as exc:
                logger.warning(
                    "Could not read system CA bundle %s: %s", sys_bundle, exc
                )
        for cert in iter_cert_files(ssl_dir):
            try:
                with open(cert) as f:
                    out.write(f.read())
                written += 1
            except OSError as exc:
                logger.warning("Skipping unreadable cert %s: %s", cert, exc)
    return written > 0 and os.path.getsize(bundle_path) > 0


class SSLTrust:
    """Owns the settings-dependent SSL trust surface (#1567).

    The 2 functions that read ``settings`` — the cert-dir resolver and the
    backend-process trust applier — live here as methods (``ssl_cert_dir`` /
    ``apply_backend_ssl_trust``), reaching the deployer config through
    ``self.settings`` rather than threading it through every call. The 4 pure
    path/bundle helpers and the module constants stay module-level: they take
    explicit paths or none at all and read no settings.
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app
        self.apply_backend_ssl_trust()

    def ssl_cert_dir(self) -> str | None:
        """Return the deployer SSL cert dir if it should be trusted, else ``None``.

        Resolves ``KLANGKD_SSL_CERT_DIR`` first (deprecated, backwards-compat);
        falls back to ``<KLANGKD_CUSTOMIZE_DIR>/certs`` (#1360).  Returns the
        absolute path when the directory exists and contains at least one
        ``.pem``/``.crt`` file; ``None`` otherwise (unset, missing, or empty
        of certs).  Never raises — a misconfigured path simply disables
        runtime trust.
        """
        raw = self.app.state.settings.ssl_cert_dir
        if not raw:
            customize = self.app.state.settings.customize_dir
            candidate = os.path.join(customize, "certs")
            if os.path.isdir(candidate):
                raw = candidate
        if not raw:
            return None
        path = os.path.realpath(raw)
        if not os.path.isdir(path):
            return None
        return path if any(True for _ in iter_cert_files(path)) else None

    def apply_backend_ssl_trust(self) -> str | None:
        """Make the backend process trust the deployer's custom CAs.

        Builds a merged bundle (system + custom) under ``<data_dir>/ssl`` and sets
        :data:`SSL_TRUST_VARS` in :data:`os.environ`, so the backend's outbound TLS
        (OIDC discovery, SMTP relay, LLM-proxy upstream) honors the private CAs.
        Idempotent and safe to call at startup; a no-op when no cert dir is
        configured.  Refuses to apply trust when the system bundle can't be found
        *and* no cert was written (would risk losing public-internet trust).

        Returns the bundle path, or ``None`` if trust was not applied.
        """
        ssl_dir = self.ssl_cert_dir()
        if not ssl_dir:
            return None
        state_dir = self.app.state.settings.state_dir
        bundle_dir = os.path.join(state_dir, "ssl")
        try:
            os.makedirs(bundle_dir, exist_ok=True)
        except OSError as exc:
            logger.error(
                "Cannot create SSL bundle dir %s: %s", bundle_dir, exc
            )
            return None
        bundle_path = os.path.join(bundle_dir, "ca-bundle.crt")
        if not write_merged_bundle(bundle_path, ssl_dir):
            logger.warning(
                "SSL bundle %s is empty (no system bundle and no custom certs); "
                "not applying backend trust",
                bundle_path,
            )
            return None
        if not system_ca_bundle(self_bundle=bundle_path):
            logger.warning(
                "Applying backend SSL trust without a system bundle: the trust "
                "vars replace the default store, so public-internet TLS endpoints "
                "may fail. Provide a system CA bundle or unset KLANGKD_SSL_CERT_DIR."
            )
        for name in SSL_TRUST_VARS:
            os.environ[name] = bundle_path
        logger.info(
            "Backend SSL trust applied: %s -> %d env var(s) (custom certs from %s)",
            bundle_path,
            len(SSL_TRUST_VARS),
            ssl_dir,
        )
        return bundle_path
