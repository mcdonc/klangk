# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## v1.0.5

### Fixed

- **Custom CA trust is now applied before Logfire's startup probe.** `setup_logfire()` previously ran at module scope (import time), before the FastAPI lifespan applied backend SSL trust, so `logfire.configure()`'s API connectivity probe fired before the `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` env vars were set. With a `LOGFIRE_BASE_URL` whose certificate is signed by a private CA supplied via `$KLANGK_CUSTOMIZE_DIR/certs/`, this caused a spurious `Logfire API is unreachable` / `CERTIFICATE_VERIFY_FAILED` warning at every startup even though the merged CA bundle and env vars were otherwise correct. `setup_logfire()` now runs in the lifespan, immediately after `apply_backend_ssl_trust()`. ([#1406](https://github.com/mcdonc/klangk/issues/1406))
