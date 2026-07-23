import { execSync, spawn } from "child_process";
import { mkdirSync, mkdtempSync, openSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

import { cleanEnv } from "./e2e-env";

async function globalSetup() {
  // When KLANGKBUILD_TEST_URL is set, skip server startup — tests run against
  // an already-running instance (e.g. the host container).
  if (process.env.KLANGKBUILD_TEST_URL) {
    const url = process.env.KLANGKBUILD_TEST_URL;
    console.log(`Using external server at ${url} — skipping server startup`);
    // Verify it's reachable
    for (let i = 0; i < 30; i++) {
      try {
        const resp = await fetch(`${url}/health`);
        if (resp.ok) {
          console.log(`External server ready`);
          return;
        }
      } catch {
        // Not ready yet
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error(`External server at ${url} not reachable after 30s`);
  }

  const dataDir = mkdtempSync(join(tmpdir(), "klangk-e2e-"));
  process.env.KLANGKBUILD_E2E_DATA_DIR = dataDir;
  const stateDir = mkdtempSync(join(tmpdir(), "klangk-e2e-state-"));
  process.env.KLANGKBUILD_E2E_STATE_DIR = stateDir;

  // Create a branding dir so setup_static_files mounts /branding.
  const customizeDir = join(dataDir, "customize");
  const brandingDir = join(customizeDir, "branding");
  mkdirSync(brandingDir, { recursive: true });

  const projectRoot = join(__dirname, "..", "..", "..");
  const backendPort = process.env.KLANGKBUILD_E2E_PORT || "18997";

  const logDir = join(projectRoot, "src", "frontend", "e2e-tests", "logs");
  mkdirSync(logDir, { recursive: true });

  // klangkd's own proxy (nginx) serves both the browser ingress (KLANGKD_PORT)
  // and the container egress (KLANGKD_EGRESS_PORT below). The previous
  // test-only LLM-proxy nginx is gone — it existed only because the old
  // runtestserver.py launch had the proxy disabled; klangkd's real proxy is
  // the production egress path (#1525).
  const proxyPort = "18995";

  console.log(
    `Starting E2E server on port ${backendPort} ` +
      `with KLANGKD_DATA_DIR=${dataDir}`,
  );

  // Determine the backend log path up front so klangkd's stdout/stderr can
  // be wired directly to the file (via an fd). klangkd's proxy (nginx) reopens
  // /dev/stdout (its access_log), which fails with ENXIO when stdout is a
  // pipe — a real file fd reopens cleanly. This mirrors the Python E2E
  // helper's log_path (#1525, #364).
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const logPath = join(logDir, `backend-${timestamp}.log`);
  process.env.KLANGKBUILD_E2E_LOG = logPath;
  const logFd = openSync(logPath, "w");

  // Start the real production server (klangkd) with the proxy enabled. The
  // browser hits the proxy on KLANGKD_PORT; the proxy proxies to klangkd's UDS,
  // exactly as in production (#1525). Replaces the test-only
  // runtestserver.py TCP launcher.
  const backendProcess = spawn(
    "python3",
    ["-m", "klangk.launcher", "--config=none"],
    {
      cwd: join(projectRoot, "src", "klangk", "klangkd-tests"),
      detached: true,
      stdio: ["ignore", logFd, logFd],
      env: cleanEnv({
        KLANGKD_PORT: backendPort,
        KLANGKD_EGRESS_PORT: proxyPort,
        KLANGKD_DATA_DIR: dataDir,
        KLANGKD_STATE_DIR: stateDir,
        KLANGKD_CUSTOMIZE_DIR: join(dataDir, "customize"),
        KLANGKD_LOGIN_LOCKOUT_FAILURES: "5",
        KLANGKD_JWT_SECRET: "e2e-test-secret",
        KLANGKD_DEFAULT_USER: "admin@example.com",
        KLANGKD_DEFAULT_PASSWORD: "admin",
        // These tests exercise the password auth flow (login, register,
        // lockout); pin password mode explicitly — the production default
        // is `none` when unset (#1374), which disables all of that.
        KLANGKD_AUTH_MODES: "password",
        KLANGKD_TEST_MODE: "1",
        KLANGKD_PORT_RANGE_START: "19200",
        LOGFIRE_TOKEN: "", // Disable Logfire tracing during E2E tests
        KLANGKD_LOGIN_BANNER_TITLE: "", // No consent banner in E2E tests
        KLANGKD_LOGIN_BANNER: "",
        KLANGKD_OIDC_CONFIG: "", // Disable OIDC providers in E2E tests
        KLANGKD_OIDC_LOGIN_HOOK: "", // No OIDC login hook in E2E tests
        KLANGKD_DISABLE_REGISTRATION: "", // Allow registration in E2E tests
        KLANGKD_DISABLE_INVITES: "", // Allow invitations in E2E tests
      }),
    },
  );

  process.env.KLANGKBUILD_E2E_PID = String(backendProcess.pid);

  const baseUrl = `http://localhost:${backendPort}`;
  const maxWait = 600;
  for (let i = 0; i < maxWait; i++) {
    try {
      const resp = await fetch(`${baseUrl}/health`);
      if (resp.ok) {
        console.log(`E2E server ready after ${i} seconds`);
        return;
      }
    } catch {
      // Server not ready yet
    }
    await new Promise((r) => setTimeout(r, 1000));
  }

  throw new Error("E2E server failed to start within 10 minutes");
}

export default globalSetup;
