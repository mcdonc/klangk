# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed

- **The per-container service-command firing-lock dict no longer accumulates orphaned entries.** `clear_service_session_lock()` covered the normal teardown path, but a racing re-bind in `stop_and_remove_container` could leave an entry whose container was already gone, so `_service_session_locks` grew one entry per container ever created until process restart. Both teardown paths (`stop_and_remove_container` and `remove_state`) now call a new `prune_service_session_locks()` that drops entries for containers no longer tracked by the registry, skipping any lock currently held so an in-flight service-command fire keeps its serialization. ([#1351](https://github.com/mcdonc/klangk/issues/1351))

## v1.0.5

### Fixed

- **Custom CA trust is now applied before Logfire's startup probe.** `setup_logfire()` previously ran at module scope (import time), before the FastAPI lifespan applied backend SSL trust, so `logfire.configure()`'s API connectivity probe fired before the `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` env vars were set. With a `LOGFIRE_BASE_URL` whose certificate is signed by a private CA supplied via `$KLANGK_CUSTOMIZE_DIR/certs/`, this caused a spurious `Logfire API is unreachable` / `CERTIFICATE_VERIFY_FAILED` warning at every startup even though the merged CA bundle and env vars were otherwise correct. `setup_logfire()` now runs in the lifespan, immediately after `apply_backend_ssl_trust()`. ([#1406](https://github.com/mcdonc/klangk/issues/1406))
