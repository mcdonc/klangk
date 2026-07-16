# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- **Option to require consent banner acceptance on every visit (#1544).**
  New setting `KLANGK_LOGIN_BANNER_EVERY_VISIT` (default `false`). When
  `true`, the login/consent banner must be re-accepted on every fresh app
  load / login — acceptance is held for the session only (in-memory), never
  persisted. When `false` (default), behavior is unchanged: acceptance is
  cached permanently against the banner text hash. Surfaced on
  `GET /api/v1/config` as `login_banner_every_visit`.

## v1.0.5

### Fixed

- **Custom CA trust is now applied before Logfire's startup probe.** `setup_logfire()` previously ran at module scope (import time), before the FastAPI lifespan applied backend SSL trust, so `logfire.configure()`'s API connectivity probe fired before the `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` env vars were set. With a `LOGFIRE_BASE_URL` whose certificate is signed by a private CA supplied via `$KLANGK_CUSTOMIZE_DIR/certs/`, this caused a spurious `Logfire API is unreachable` / `CERTIFICATE_VERIFY_FAILED` warning at every startup even though the merged CA bundle and env vars were otherwise correct. `setup_logfire()` now runs in the lifespan, immediately after `apply_backend_ssl_trust()`. ([#1406](https://github.com/mcdonc/klangk/issues/1406))
