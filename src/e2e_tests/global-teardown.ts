import { execSync } from "child_process";
import { existsSync, rmSync } from "fs";
import { join } from "path";

async function globalTeardown() {
  const projectRoot = join(__dirname, "..", "..");

  const pid = process.env.BARK_E2E_PID;
  if (pid) {
    const numPid = Number(pid);
    console.log(`Stopping E2E server (PID ${numPid})...`);

    // Kill the process group (spawned with detached: true)
    try {
      process.kill(-numPid, "SIGTERM");
    } catch {
      // Process group doesn't exist
    }

    // Wait up to 30s for graceful shutdown (needs time to stop all containers)
    let alive = true;
    for (let i = 0; i < 60; i++) {
      try {
        process.kill(numPid, 0);
        await new Promise((r) => setTimeout(r, 500));
      } catch {
        alive = false;
        break;
      }
    }

    // Force kill if still alive
    if (alive) {
      console.warn("Server did not exit gracefully, sending SIGKILL");
      try {
        process.kill(-numPid, "SIGKILL");
      } catch {
        // Already dead
      }
    }
  }

  // Remove any containers that survived shutdown (including stopped ones holding ports)
  try {
    const ids = execSync('docker ps -a --filter "label=bark.managed=true" -q')
      .toString()
      .trim();
    if (ids) {
      execSync(`docker rm -f ${ids.split("\n").join(" ")}`);
      console.log("Removed leftover bark containers");
    }
  } catch {
    // Docker not available or no containers
  }

  // Print backend log location
  const logPath = process.env.BARK_E2E_LOG;
  if (logPath && existsSync(logPath)) {
    console.log(`Backend log: ${logPath}`);
  }

  // Clean up temp data directory. On CI, Docker containers create root-owned
  // files in bind-mounted workspace dirs, so rmSync fails with EACCES.
  // Use a Docker container to remove them first.
  const dataDir = process.env.BARK_E2E_DATA_DIR;
  if (dataDir) {
    console.log(`Cleaning up ${dataDir}`);
    try {
      if (process.env.CI) {
        execSync(
          `docker run --rm -v "${dataDir}:/cleanup" alpine rm -rf /cleanup/*`,
          { stdio: "ignore", timeout: 10_000 },
        );
      }
      rmSync(dataDir, { recursive: true, force: true });
    } catch {
      // Best effort — CI will clean up the runner anyway
    }
  }

  console.log("E2E teardown complete");
}

export default globalTeardown;
