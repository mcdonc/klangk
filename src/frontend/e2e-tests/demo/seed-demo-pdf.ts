/**
 * Seed the Pyramid PDF into the hero's `demo` workspace home.
 *
 * Run this RIGHT AFTER the demo container is created (Scene 2's
 * `klangkc create demo`), so the file is present for Scene 6 (File Browser) to
 * browse. Idempotent — a re-run just overwrites. Called by record-cli.sh after
 * the CLI pass; also safe to run standalone for a browser-only continuation:
 *
 *   devenv shell -- node --experimental-strip-types \
 *     src/frontend/e2e-tests/demo/seed-demo-pdf.ts
 *
 * Standalone (no Playwright): uses node's global fetch + child_process. Logs
 * in as the hero, finds `demo`, resolves the container's $HOME off-camera via
 * `klangkc exec`, and uploads the PDF via the files/upload API (which needs the
 * container running).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const DEMO_URL = process.env.KLANGK_TEST_URL || "http://localhost:8996";
const HERO = process.env.KLANGK_DEMO_ADMIN_EMAIL || "admin@example.com";
const PASS = process.env.KLANGK_DEMO_ADMIN_PASSWORD || "adminpass";
const WS = process.env.KLANGK_DEMO_WORKSPACE || "demo";
const __dirname = dirname(fileURLToPath(import.meta.url));
const PDF = join(__dirname, "assets", "pyramid-docs.pdf");

async function login(): Promise<string> {
  const r = await fetch(`${DEMO_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: HERO, password: PASS }),
  });
  if (!r.ok) throw new Error(`hero login failed: ${r.status}`);
  return (await r.json()).access_token;
}

async function findWorkspace(token: string): Promise<string> {
  const r = await fetch(`${DEMO_URL}/api/v1/workspaces`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const list = (await r.json()) as Array<{ name: string; id: string }>;
  const found = list.find((w) => w.name === WS);
  if (!found)
    throw new Error(`workspace "${WS}" not found — run Scene 2 first`);
  return found.id;
}

function containerHome(name: string): string {
  // klangkc exec resolves the hero's per-user home symlink off-camera.
  const server = DEMO_URL;
  return execFileSync(
    "klangkc",
    ["--server", server, "exec", name, "bash", "-lc", "echo -n $HOME"],
    { encoding: "utf-8" },
  ).trim();
}

async function upload(token: string, id: string, home: string): Promise<void> {
  const path = `${home}/pyramid-docs.pdf`;
  const form = new FormData();
  form.append(
    "file",
    new Blob([readFileSync(PDF)], { type: "application/pdf" }),
    "pyramid-docs.pdf",
  );
  const r = await fetch(
    `${DEMO_URL}/api/v1/workspaces/${id}/files/upload?path=${encodeURIComponent(path)}`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    },
  );
  if (!r.ok) throw new Error(`upload failed: ${r.status} ${await r.text()}`);
  console.log(`  ✓ seeded pyramid-docs.pdf → ${path}`);
}

const token = await login();
const id = await findWorkspace(token);
const home = containerHome(WS);
await upload(token, id, home);
console.log("Seed-PDF complete.");
