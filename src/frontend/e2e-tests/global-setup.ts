import { spawn } from "child_process";
import { createWriteStream, mkdirSync, mkdtempSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

async function globalSetup() {
  const dataDir = mkdtempSync(join(tmpdir(), "klangk-e2e-"));
  process.env.KLANGK_E2E_DATA_DIR = dataDir;

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

  // Start nginx as an LLM proxy so containers can reach the LLM.
  // Containers are configured with KLANGK_LLM_PROXY_URL pointing at this nginx.
  const nginxPort = "18995";
  if (llmUrl) {
    const nginxLogPath = join(
      logDir,
      `nginx-${new Date().toISOString().replace(/[:.]/g, "-")}.log`,
    );
    const nginxLogFd = require("fs").openSync(nginxLogPath, "w");
    const nginxProcess = spawn(join(projectRoot, "scripts", "nginx.sh"), [], {
      detached: true,
      stdio: ["ignore", nginxLogFd, nginxLogFd],
      env: {
        ...process.env,
        DEVENV_STATE: dataDir,
        KLANGK_NGINX_PORT: nginxPort,
        KLANGK_PORT: backendPort,
      },
    });
    process.env.KLANGK_E2E_NGINX_PID = String(nginxProcess.pid);
    // Wait briefly for nginx to start
    await new Promise((r) => setTimeout(r, 1000));
    console.log(
      `LLM proxy nginx started on port ${nginxPort} (log: ${nginxLogPath})`,
    );
  }

  console.log(
    `Starting E2E server on port ${backendPort} ` +
      `with KLANGK_DATA_DIR=${dataDir}`,
  );

  // Start uvicorn directly with E2E overrides as env vars.
  const backendProcess = spawn(
    "uvicorn",
    ["klangk_backend.main:app", "--host", "0.0.0.0", "--port", backendPort],
    {
      cwd: join(projectRoot, "src", "backend"),
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        KLANGK_PORT: backendPort,
        KLANGK_NGINX_PORT: nginxPort,
        KLANGK_DATA_DIR: dataDir,
        KLANGK_LOGIN_LOCKOUT_FAILURES: "5",
        KLANGK_JWT_SECRET: "e2e-test-secret",
        KLANGK_DEFAULT_USER: "admin@example.com",
        KLANGK_DEFAULT_PASSWORD: "admin",
        KLANGK_TEST_MODE: "1",
        KLANGK_INSTANCE_ID: "e2e-test",
        KLANGK_PORT_RANGE_START: "19200",
        LOGFIRE_TOKEN: "", // Disable Logfire tracing during E2E tests
        KLANGK_LOGIN_BANNER_TITLE: "", // No consent banner in E2E tests
        KLANGK_LOGIN_BANNER: "",
      },
    },
  );

  process.env.KLANGK_E2E_PID = String(backendProcess.pid);

  // Write backend output to a per-run log file so logs aren't overwritten
  // when test-e2e runs each browser sequentially.
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const logPath = join(logDir, `backend-${timestamp}.log`);
  const logStream = createWriteStream(logPath);
  process.env.KLANGK_E2E_LOG = logPath;
  backendProcess.stdout?.pipe(logStream);
  backendProcess.stderr?.pipe(logStream);

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
