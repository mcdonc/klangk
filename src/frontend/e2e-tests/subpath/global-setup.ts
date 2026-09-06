import { execSync, spawn } from "child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "fs";
import { tmpdir } from "os";
import { join } from "path";

import { ADMIN_PASSWORD, cleanEnv } from "../e2e-env";

// The subpath × web × DPoP stack (#3287): a second klangkd serving the
// Flutter build with a `/klangk` base href, fronted by an outer caddy
// that strips the prefix and sends `X-Forwarded-Prefix: /klangk` — the
// documented nginx pattern from docs/deployment/behind-a-proxy.md, at
// browser-facing /klangk/.
//
// Ports (main suite: backend 18997, egress 18995, port range 19200+):
// - 18998: outer caddy (the URL the browser uses)
// - 18999: klangkd browser listener (what the outer caddy forwards to)
// - 18996: klangkd container egress listener
// - 23200+: workspace port range (far from the main suite's 19200+)
// No trailing slash: the setup appends paths to it directly.
export const SUBPATH_BASE_URL =
  process.env.KLANGKBUILD_SUBPATH_URL || "http://localhost:18998/klangk";
export const SUBPATH_PREFIX = "/klangk";

const BACKEND_PORT = "18999";
const EGRESS_PORT = "18996";
const OUTER_PORT = "18998";

function subpathWebDir(): string {
  // A copy of the built frontend with the base href rewritten to the
  // subpath — the asset-URL shape of a `flutter build web
  // --base-href=/klangk/` build (the docs' outer-nginx sub_filter does
  // the same rewrite in flight). Lives under the e2e logs dir, which is
  // gitignored; rebuilt on every run so it never goes stale.
  const logsDir = join(__dirname, "..", "logs");
  mkdirSync(logsDir, { recursive: true });
  return join(logsDir, "subpath-web");
}

function prepareSubpathFrontend(): string {
  const projectRoot = join(__dirname, "..", "..", "..", "..");
  const built = join(projectRoot, "src", "frontend", "build", "web");
  const indexHtml = join(built, "index.html");
  if (!existsSync(indexHtml)) {
    throw new Error(
      `Flutter build not found at ${built} — run ` +
        `'devenv tasks run klangk:flutter-build' first`,
    );
  }
  const target = subpathWebDir();
  rmSync(target, { recursive: true, force: true });
  cpSync(built, target, { recursive: true });

  // Rewrite the base tag. Flutter emits `<base href="/" />`; accept
  // either attribute spacing and fail loudly when the shape drifts.
  const indexPath = join(target, "index.html");
  const html = readFileSync(indexPath, "utf8");
  const rewritten = html.replace(
    /<base href="\/"\s*\/?>/,
    `<base href="${SUBPATH_PREFIX}/">`,
  );
  if (rewritten === html) {
    throw new Error(
      `Could not find '<base href="/">' in ${indexPath} — the build's ` +
        `base tag shape changed; update the rewrite`,
    );
  }
  writeFileSync(indexPath, rewritten);
  return target;
}

function writeOuterCaddyfile(): string {
  // The outer proxy of the documented subpath pattern: strip the
  // prefix, tell klangkd about it via X-Forwarded-Prefix (trusted:
  // klangkd's default trusted-proxy set is loopback, and caddy dials
  // 127.0.0.1), forward everything else — /api, /ws (upgrades
  // included), /hosted, documents, assets.
  const path = join(subpathWebDir(), "..", "subpath-Caddyfile");
  writeFileSync(
    path,
    [
      "{",
      "\tauto_https off",
      "\tadmin off",
      "}",
      `http://:${OUTER_PORT} {`,
      "\tbind 127.0.0.1",
      `\tredir /klangk /klangk/ 308`,
      "\thandle_path /klangk/* {",
      "\t\treverse_proxy 127.0.0.1:" + BACKEND_PORT + " {",
      `\t\t\theader_up X-Forwarded-Prefix ${SUBPATH_PREFIX}`,
      "\t\t}",
      "\t}",
      "\trespond 404",
      "}",
      "",
    ].join("\n"),
  );
  return path;
}

async function waitHealthy(url: string, label: string): Promise<void> {
  for (let i = 0; i < 120; i++) {
    try {
      const resp = await fetch(url);
      if (resp.ok) return;
    } catch {
      // Not ready yet
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`${label} not reachable at ${url} within 120s`);
}

function stackHolderPids(port: number): string[] {
  try {
    return execSync(`fuser ${port}/tcp 2>/dev/null`, { encoding: "utf8" })
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .filter((pid) => {
        try {
          const cmdline = readFileSync(`/proc/${pid}/cmdline`, "utf8");
          return cmdline.includes("klangk.main") || cmdline.includes("caddy");
        } catch {
          return false;
        }
      });
  } catch {
    return [];
  }
}

function portHeld(port: number): boolean {
  try {
    execSync(`fuser ${port}/tcp 2>/dev/null`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function anyStackPortHeld(): boolean {
  return [BACKEND_PORT, EGRESS_PORT, OUTER_PORT].some(portHeld);
}

function signalStackHolders(signal: string): void {
  for (const port of [BACKEND_PORT, EGRESS_PORT, OUTER_PORT]) {
    for (const pid of stackHolderPids(port)) {
      execSync(`kill ${signal} ${pid} 2>/dev/null`, { stdio: "ignore" });
    }
  }
}

async function freeStackPorts(): Promise<void> {
  // A crashed earlier run can leave this stack's klangkd (or its caddy)
  // wedged on the ports. Teardown normally reaches klangkd's caddy too
  // (it carries PDEATHSIG), but a hard-crashed run leaves nothing behind
  // to kill it. Only holders that ARE this stack — a python running
  // klangk.main, or a caddy — are killed, so an unrelated process that
  // happens to hold a port is left alone (the spawn then fails loudly).
  signalStackHolders("");
  // A SIGTERM'd klangkd drains workspaces before exiting (seconds, with a
  // container up), and its caddy follows via PDEATHSIG — wait for the ports
  // to actually release so the respawn below does not lose the bind race
  // (or worse, have /health answered by the still-draining old backend).
  for (let i = 0; i < 40 && anyStackPortHeld(); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
  // Still draining after 10s — take the stack's processes down hard.
  signalStackHolders("-9");
  for (let i = 0; i < 20 && anyStackPortHeld(); i++) {
    await new Promise((r) => setTimeout(r, 250));
  }
}

async function globalSetup() {
  await freeStackPorts();
  const projectRoot = join(__dirname, "..", "..", "..", "..");
  const dataDir = mkdtempSync(join(tmpdir(), "klangk-subpath-e2e-"));
  const stateDir = mkdtempSync(join(tmpdir(), "klangk-subpath-e2e-state-"));
  const frontendDir = prepareSubpathFrontend();
  const caddyfile = writeOuterCaddyfile();

  const logDir = join(__dirname, "..", "logs");
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const backendLog = join(logDir, `subpath-backend-${timestamp}.log`);
  const outerLog = join(logDir, `subpath-outer-${timestamp}.log`);
  const backendFd = openSync(backendLog, "w");
  const outerFd = openSync(outerLog, "w");

  console.log(
    `Starting subpath stack: klangkd on :${BACKEND_PORT} (frontend ` +
      `${frontendDir}), outer caddy on :${OUTER_PORT} at ${SUBPATH_PREFIX}/`,
  );

  const backend = spawn("python3", ["-m", "klangk.main", "--config=none"], {
    cwd: join(projectRoot, "src", "klangk", "klangkd-tests"),
    detached: true,
    stdio: ["ignore", backendFd, backendFd],
    env: cleanEnv({
      KLANGKD_PORT: BACKEND_PORT,
      KLANGKD_EGRESS_PORT: EGRESS_PORT,
      KLANGKD_DATA_DIR: dataDir,
      KLANGKD_STATE_DIR: stateDir,
      KLANGKD_FRONTEND_DIR: frontendDir,
      KLANGKD_CUSTOMIZE_DIR: join(dataDir, "customize"),
      KLANGKD_API_RATE_LIMIT: "0",
      KLANGKD_JWT_SECRET: "e2e-subpath-test-secret",
      KLANGKD_DEFAULT_USER: "admin@example.com",
      KLANGKD_DEFAULT_PASSWORD: ADMIN_PASSWORD,
      KLANGKD_AUTH_MODES: "password",
      KLANGKD_TEST_MODE: "1",
      KLANGKD_PORT_RANGE_START: "23200",
      KLANGKD_LOGIN_BANNER_TITLE: "",
      KLANGKD_LOGIN_BANNER: "",
      KLANGKD_OIDC_CONFIG: "",
      KLANGKD_OIDC_LOGIN_HOOK: "",
      KLANGKD_DISABLE_REGISTRATION: "",
      KLANGKD_DISABLE_INVITES: "",
      LOGFIRE_TOKEN: "",
    }),
  });
  process.env.KLANGKBUILD_SUBPATH_BACKEND_PID = String(backend.pid);
  process.env.KLANGKBUILD_SUBPATH_BACKEND_LOG = backendLog;

  const outer = spawn(
    "caddy",
    ["run", "--config", caddyfile, "--adapter", "caddyfile"],
    {
      cwd: logDir,
      detached: true,
      stdio: ["ignore", outerFd, outerFd],
    },
  );
  process.env.KLANGKBUILD_SUBPATH_OUTER_PID = String(outer.pid);
  process.env.KLANGKBUILD_SUBPATH_OUTER_LOG = outerLog;

  await waitHealthy(
    `http://localhost:${BACKEND_PORT}/health`,
    "subpath klangkd",
  );
  // Health THROUGH the outer proxy proves the prefix strip end to end.
  await waitHealthy(`${SUBPATH_BASE_URL}/health`, "subpath outer caddy chain");

  console.log(`Subpath stack ready at ${SUBPATH_BASE_URL}/`);
  console.log(`Backend log: ${backendLog}`);
  console.log(`Outer log: ${outerLog}`);
}

export default globalSetup;
