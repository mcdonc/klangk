import { execSync, spawn } from "child_process";
import { mkdirSync, mkdtempSync, openSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

import { cleanEnv } from "./e2e-env";

async function globalSetup() {
  // When KLANGK_TEST_URL is set, skip server startup — tests run against
  // an already-running instance (e.g. the host container).
  if (process.env.KLANGK_TEST_URL) {
    const url = process.env.KLANGK_TEST_URL;
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
  process.env.KLANGK_E2E_DATA_DIR = dataDir;
  const stateDir = mkdtempSync(join(tmpdir(), "klangk-e2e-state-"));
  process.env.KLANGK_E2E_STATE_DIR = stateDir;

  // Create a branding dir so setup_static_files mounts /branding.
  const customizeDir = join(dataDir, "customize");
  const brandingDir = join(customizeDir, "branding");
  mkdirSync(brandingDir, { recursive: true });

  const projectRoot = join(__dirname, "..", "..", "..");
  const backendPort = process.env.KLANGK_E2E_PORT || "18997";

  // Warm up LLM before starting the server to force model loading.
  // Cold model loads on first request can take 30+ seconds.
  const llmUrl = process.env.KLANGK_KLANGK_LLM_BASE_URL;
  const llmModel = process.env.KLANGK_KLANGK_LLM_MODEL;
  const llmKey = process.env.KLANGK_LLM_API_KEY;
  if (llmUrl && llmModel) {
    console.log("Warming up LLM...");
    const warmupStart = Date.now();
    try {
      const warmupResp = await fetch(`${llmUrl}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(llmKey ? { Authorization: `Bearer ${llmKey}` } : {}),
        },
        body: JSON.stringify({
          model: llmModel,
          messages: [{ role: "user", content: "hi" }],
          max_tokens: 1,
        }),
      });
      if (warmupResp.ok) {
        console.log(
          `LLM warm (${((Date.now() - warmupStart) / 1000).toFixed(1)}s)`,
        );
      } else {
        throw new Error(
          `LLM warmup failed: ${warmupResp.status} — check KLANGK_LLM_BASE_URL and KLANGK_LLM_MODEL are set correctly`,
        );
      }
    } catch (e) {
      throw new Error(
        `LLM warmup error: ${e} — check KLANGK_LLM_BASE_URL and KLANGK_LLM_MODEL are set correctly`,
      );
    }
  } else if (llmUrl) {
    throw new Error(
      "KLANGK_LLM_BASE_URL is set but KLANGK_LLM_MODEL is not — add KLANGK_LLM_MODEL to .env",
    );
  } else {
    // No LLM configured — skip warmup.
    console.log("LLM not configured — skipping warmup");
  }

  const logDir = join(projectRoot, "src", "frontend", "e2e-tests", "logs");
  mkdirSync(logDir, { recursive: true });

  // klangkd's own nginx serves both the browser ingress (KLANGK_PORT)
  // and the container egress (KLANGK_EGRESS_PORT below). The previous
  // test-only LLM-proxy nginx is gone — it existed only because the old
  // runtestserver.py launch had nginx disabled; klangkd's real nginx is
  // the production egress path (#1525).
  const nginxPort = "18995";

  console.log(
    `Starting E2E server on port ${backendPort} ` +
      `with KLANGK_DATA_DIR=${dataDir}`,
  );

  // Determine the backend log path up front so klangkd's stdout/stderr can
  // be wired directly to the file (via an fd). klangkd's nginx reopens
  // /dev/stdout (its access_log), which fails with ENXIO when stdout is a
  // pipe — a real file fd reopens cleanly. This mirrors the Python E2E
  // helper's log_path (#1525, #364).
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const logPath = join(logDir, `backend-${timestamp}.log`);
  process.env.KLANGK_E2E_LOG = logPath;
  const logFd = openSync(logPath, "w");

  // Start the real production server (klangkd) with nginx enabled. The
  // browser hits nginx on KLANGK_PORT; nginx proxies to klangkd's UDS,
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
        KLANGK_PORT: backendPort,
        KLANGK_EGRESS_PORT: nginxPort,
        KLANGK_DATA_DIR: dataDir,
        KLANGK_STATE_DIR: stateDir,
        KLANGK_CUSTOMIZE_DIR: join(dataDir, "customize"),
        KLANGK_LOGIN_LOCKOUT_FAILURES: "5",
        KLANGK_JWT_SECRET: "e2e-test-secret",
        KLANGK_DEFAULT_USER: "admin@example.com",
        KLANGK_DEFAULT_PASSWORD: "admin",
        // These tests exercise the password auth flow (login, register,
        // lockout); pin password mode explicitly — the production default
        // is `none` when unset (#1374), which disables all of that.
        KLANGK_AUTH_MODES: "password",
        KLANGK_TEST_MODE: "1",
        KLANGK_PORT_RANGE_START: "19200",
        LOGFIRE_TOKEN: "", // Disable Logfire tracing during E2E tests
        KLANGK_LOGIN_BANNER_TITLE: "", // No consent banner in E2E tests
        KLANGK_LOGIN_BANNER: "",
        KLANGK_OIDC_CONFIG: "", // Disable OIDC providers in E2E tests
        KLANGK_OIDC_LOGIN_HOOK: "", // No OIDC login hook in E2E tests
        KLANGK_DISABLE_REGISTRATION: "", // Allow registration in E2E tests
        KLANGK_DISABLE_INVITES: "", // Allow invitations in E2E tests
      }),
    },
  );

  process.env.KLANGK_E2E_PID = String(backendProcess.pid);

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
