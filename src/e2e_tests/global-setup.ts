import { spawn } from "child_process";
import { createWriteStream, mkdtempSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

async function globalSetup() {
  const dataDir = mkdtempSync(join(tmpdir(), "bark-e2e-"));
  process.env.BARK_E2E_DATA_DIR = dataDir;

  const projectRoot = join(__dirname, "..", "..");
  const backendPort = process.env.BARK_E2E_PORT || "18997";

  console.log(
    `Starting E2E server on port ${backendPort} ` +
      `with BARK_DATA_DIR=${dataDir}`,
  );

  // Start uvicorn directly with E2E overrides as env vars.
  // No devenv up or nginx needed — just the backend server.
  const backendProcess = spawn(
    "uvicorn",
    ["bark_backend.main:app", "--host", "0.0.0.0", "--port", backendPort],
    {
      cwd: join(projectRoot, "src", "backend"),
      stdio: ["ignore", "pipe", "pipe"],
      env: {
        ...process.env,
        BARK_PORT: backendPort,
        BARK_DATA_DIR: dataDir,
        BARK_JWT_SECRET: "e2e-test-secret",
        BARK_DEFAULT_USER: "admin",
        BARK_DEFAULT_PASSWORD: "admin",
        BARK_TEST_MODE: "1",
      },
    },
  );

  process.env.BARK_E2E_PID = String(backendProcess.pid);

  // Write backend output to a log file to keep Playwright output clean
  const logPath = join(projectRoot, "src", "e2e_tests", "backend.log");
  const logStream = createWriteStream(logPath);
  process.env.BARK_E2E_LOG = logPath;
  backendProcess.stdout?.pipe(logStream);
  backendProcess.stderr?.pipe(logStream);

  const baseUrl = `http://localhost:${backendPort}`;
  const maxWait = 600;
  for (let i = 0; i < maxWait; i++) {
    try {
      const resp = await fetch(`${baseUrl}/api/config`);
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
