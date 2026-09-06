import { existsSync } from "fs";

async function killTree(pidEnv: string, label: string): Promise<void> {
  const pid = Number(process.env[pidEnv]);
  if (!pid) return;
  console.log(`Stopping ${label} (PID ${pid})...`);
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    return;
  }
  for (let i = 0; i < 20; i++) {
    try {
      process.kill(pid, 0);
      await new Promise((r) => setTimeout(r, 500));
    } catch {
      return;
    }
  }
  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    // Already dead
  }
}

async function globalTeardown() {
  // Kill the outer proxy first so no new request reaches the backend
  // while it drains.
  await killTree("KLANGKBUILD_SUBPATH_OUTER_PID", "subpath outer caddy");
  await killTree("KLANGKBUILD_SUBPATH_BACKEND_PID", "subpath klangkd");

  const backendLog = process.env.KLANGKBUILD_SUBPATH_BACKEND_LOG;
  if (backendLog && existsSync(backendLog)) {
    console.log(`Backend log: ${backendLog}`);
  }
  const outerLog = process.env.KLANGKBUILD_SUBPATH_OUTER_LOG;
  if (outerLog && existsSync(outerLog)) {
    console.log(`Outer log: ${outerLog}`);
  }
}

export default globalTeardown;
