/**
 * Hermetic env helper for E2E test suites (#1526).
 *
 * Mirrors `src/klangk/klangkd-tests/e2e-tests/_e2e_env.py`. Strips every config-affecting
 * prefix (KLANGK, _KLANGK, KLANGKC, LOGFIRE) from the ambient env so stray
 * vars can't leak into a test subprocess, then applies E2E baseline defaults
 * and the caller's overrides.
 */

const STRIP_PREFIXES = ["KLANGK", "_KLANGK", "KLANGKC", "LOGFIRE"];

// Build-infra vars that locate artifacts the test must use (the workspace
// container image, version stamp) — produced by devenv's
// klangk:build-workspace-image task, not by any test. Forwarded deliberately
// so the server finds the built image. (KLANGKBUILD_PLUGINS_DIR was removed
// in #1660/#1665 — the runtime reads features.json from the frontend bundle,
// not feature trees.)
//
// KLANGKD_FRONTEND_DIR is NOT here: devenv no longer exports it (#1788; it's
// a klangkd.yaml setting), so each env-only launcher sets it explicitly
// (global-setup.ts, run-demo-backend.sh). Forwarding a stray ambient value
// risks pointing at a stale/wrong build.
/**
 * The seeded admin password for E2E servers (KLANGKD_DEFAULT_PASSWORD).
 *
 * Must satisfy the boot-time password-policy gate (#2581): the daemon
 * refuses to start when the seeded admin's password violates
 * KLANGKD_MIN_PASSWORD_LENGTH (default 8) or any character-class count
 * (KLANGKD_PASSWORD_REQUIRE_*). "Admin123!" clears the length minimum and
 * all four classes, so it survives a policy tightening without a silent
 * 10-minute startup timeout in global-setup.
 */
export const ADMIN_PASSWORD = "Admin123!";

const INFRA_VARS = ["KLANGKD_IMAGE_NAME", "KLANGKD_VERSION_FILE"];

export function cleanEnv(
  overrides: Record<string, string> = {},
): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    const upper = key.toUpperCase();
    if (STRIP_PREFIXES.some((p) => upper.startsWith(p))) continue;
    env[key] = value;
  }
  // Forward build-infra vars (image / version stamp / frontend).
  for (const name of INFRA_VARS) {
    const val = process.env[name];
    if (val !== undefined) env[name] = val;
  }
  // E2E baseline defaults. the proxy is NOT disabled: the frontend suite
  // launches real klangkd, fronted by its own proxy on KLANGKD_PORT (the
  // browser hits the proxy, which proxies to klangkd's UDS — #1525).
  if (!env.KLANGKD_AUTH_MODES) env.KLANGKD_AUTH_MODES = "password";
  Object.assign(env, overrides);
  return env;
}
