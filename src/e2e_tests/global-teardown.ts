import { existsSync, readFileSync, rmSync } from "fs";
import { join } from "path";

async function globalTeardown() {
  const projectRoot = join(__dirname, "..", "..");

  const pid = process.env.BARK_E2E_PID;
  if (pid) {
    const numPid = Number(pid);
    console.log(`Stopping E2E server (PID ${numPid})...`);
    try {
      // Kill the process group so children (backend, nginx) also die
      process.kill(-numPid, "SIGTERM");
    } catch {
      try {
        process.kill(numPid, "SIGTERM");
      } catch {
        // Already dead
      }
    }

    // Wait up to 10s for it to exit
    for (let i = 0; i < 20; i++) {
      try {
        process.kill(numPid, 0); // check if alive
        await new Promise((r) => setTimeout(r, 500));
      } catch {
        break; // dead
      }
    }
  }

  // Print backend log location (log persists until dataDir cleanup)
  const logPath = process.env.BARK_E2E_LOG;
  if (logPath && existsSync(logPath)) {
    console.log(`Backend log: ${logPath}`);
  }

  // Clean up temp data directory
  const dataDir = process.env.BARK_E2E_DATA_DIR;
  if (dataDir) {
    console.log(`Cleaning up ${dataDir}`);
    try {
      rmSync(dataDir, { recursive: true, force: true });
    } catch (e) {
      console.warn(`Failed to clean up ${dataDir}:`, e);
    }
  }

  console.log("E2E teardown complete");
}

export default globalTeardown;
