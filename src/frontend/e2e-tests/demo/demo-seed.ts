/**
 * One-time seed for the demo video.
 *
 * Creates the shared fixtures the scenes assume exist:
 *   - a handful of virtual users (teammate / designer / reviewer), and
 *   - several workspaces, some shared across users, so every account's
 *     workspace list looks populated and the Sharing UI is meaningful.
 *
 * Run once before recording:
 *
 *   devenv shell -- node --experimental-strip-types \
 *       src/frontend/e2e-tests/demo/demo-seed.ts
 *
 * Idempotent: re-running is safe. Users are created if missing (via the admin
 * endpoint, so this works even if public registration is disabled). Workspaces
 * are find-or-create by name, and role grants are re-applied harmlessly.
 *
 * The admin account is NOT created here — it comes from the server's
 * KLANGK_DEFAULT_USER. Override anything with env vars: KLANGK_TEST_URL,
 * KLANGK_DEMO_PASSWORD, KLANGK_DEMO_ADMIN_PASSWORD, KLANGK_DEMO_*_EMAIL.
 */
const DEMO_URL = process.env.KLANGK_TEST_URL || "http://localhost:8995";
const ADMIN_EMAIL =
  process.env.KLANGK_DEMO_ADMIN_EMAIL ||
  process.env.KLANGK_DEFAULT_USER ||
  "admin@example.com";
const ADMIN_PASSWORD =
  process.env.KLANGK_DEMO_ADMIN_PASSWORD ||
  process.env.KLANGK_DEFAULT_PASSWORD ||
  "admin";
const DEMO_PASSWORD = process.env.KLANGK_DEMO_PASSWORD || "demopass123";

// Virtual users. Each is created via admin (idempotent) and given a personal
// workspace + membership in shared ones below.
const USERS = {
  teammate: process.env.KLANGK_DEMO_TEAMMATE_EMAIL || "teammate@example.com",
  designer: process.env.KLANGK_DEMO_DESIGNER_EMAIL || "designer@example.com",
  reviewer: process.env.KLANGK_DEMO_REVIEWER_EMAIL || "reviewer@example.com",
};

type Role = "collaborators" | "spectators";

// Workspaces to ensure. `owner` creates it (find-or-create by name). `shares`
// grants the listed users a role on it (the owner grants — owners hold `*`).
interface SeedWs {
  name: string;
  owner: "admin" | keyof typeof USERS;
  shares?: Partial<Record<keyof typeof USERS, Role>>;
}

const WORKSPACES: SeedWs[] = [
  {
    name: "Shared Workspace",
    owner: "admin",
    shares: {
      teammate: "collaborators",
      designer: "collaborators",
      reviewer: "spectators",
    },
  },
  {
    name: "Team Project",
    owner: "admin",
    shares: { teammate: "collaborators" },
  },
  {
    name: "Design Review",
    owner: "admin",
    shares: { designer: "collaborators", reviewer: "spectators" },
  },
  { name: "Teammate Sandbox", owner: "teammate" },
  { name: "Design Lab", owner: "designer" },
];

async function req(
  method: string,
  path: string,
  body: unknown,
  bearer?: string,
) {
  const resp = await fetch(`${DEMO_URL}/api/v1${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  return { ok: resp.ok, status: resp.status, body: text };
}
const post = (p: string, b: unknown, t?: string) => req("POST", p, b, t);
const get = (p: string, t?: string) => req("GET", p, undefined, t);

async function login(email: string, password: string): Promise<string> {
  const r = await post("/auth/login", { email, password });
  if (!r.ok) throw new Error(`login failed for ${email} (${r.status})`);
  return JSON.parse(r.body).access_token;
}

/** Find-or-create a workspace by name, acting as `token`. Returns its id. */
async function ensureWorkspace(
  name: string,
  token: string,
): Promise<{ id: string; created: boolean }> {
  const list = await get("/workspaces", token);
  if (list.ok) {
    const items = JSON.parse(list.body);
    const arr = Array.isArray(items) ? items : (items.items ?? []);
    const found = arr.find((w: { name?: string }) => w && w.name === name);
    if (found) return { id: found.id, created: false };
  }
  const created = await post("/workspaces", { name }, token);
  if (!created.ok) {
    throw new Error(
      `create workspace "${name}" failed (${created.status}): ${created.body.slice(0, 160)}`,
    );
  }
  return { id: JSON.parse(created.body).id, created: true };
}

/** Grant a role; ignore "already a member" style errors (idempotent). */
async function grantRole(
  wsId: string,
  role: Role,
  email: string,
  token: string,
): Promise<boolean> {
  const r = await post(`/workspaces/${wsId}/roles/${role}`, { email }, token);
  return r.ok;
}

async function ensureUser(email: string, adminToken: string): Promise<boolean> {
  // Try login first (already exists); fall back to admin-create.
  const loginTry = await post("/auth/login", {
    email,
    password: DEMO_PASSWORD,
  });
  if (loginTry.ok) return false;
  const create = await post(
    "/admin/users",
    { email, password: DEMO_PASSWORD },
    adminToken,
  );
  if (!create.ok && !/already|exists/i.test(create.body)) {
    throw new Error(
      `create user ${email} failed (${create.status}): ${create.body.slice(0, 160)}`,
    );
  }
  return true;
}

async function seed() {
  console.log(`Demo server: ${DEMO_URL}`);
  console.log(`Admin:       ${ADMIN_EMAIL}`);

  const adminToken = await login(ADMIN_EMAIL, ADMIN_PASSWORD);
  console.log("  ✓ admin login OK");

  // 1. Virtual users.
  for (const [label, email] of Object.entries(USERS)) {
    const created = await ensureUser(email, adminToken);
    console.log(`  ${created ? "✓ created" : "✓ exists "} ${label}: ${email}`);
  }

  // Tokens for each user (for owned-workspace creation).
  const tokens: Record<string, string> = { admin: adminToken };
  for (const [label, email] of Object.entries(USERS)) {
    tokens[label] = await login(email, DEMO_PASSWORD);
  }

  // 2. Workspaces + shares.
  for (const ws of WORKSPACES) {
    const ownerToken = tokens[ws.owner];
    const { id, created } = await ensureWorkspace(ws.name, ownerToken);
    console.log(
      `  ${created ? "✓ created" : "✓ exists "} workspace "${ws.name}" (owner: ${ws.owner})`,
    );
    for (const [user, role] of Object.entries(ws.shares ?? {})) {
      const email = USERS[user as keyof typeof USERS];
      // Grant via the owner's token (owners hold the `share` permission).
      const ok = await grantRole(id, role as Role, email, ownerToken);
      console.log(`      ${ok ? "✓" : "·"} ${user} → ${role} (${email})`);
    }
  }

  console.log("\nSeed complete. Next, record a scene, e.g.:");
  console.log(
    "  devenv shell -- src/frontend/e2e-tests/demo/record-demo.sh -g clanker",
  );
}

seed().catch((e) => {
  console.error("Seed failed:", e.message || e);
  process.exit(1);
});
