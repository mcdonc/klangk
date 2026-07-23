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
 * For a fully clean slate (the recording prep path), pass --reset: it deletes
 * the hero + cast users first (cascading ALL their workspaces + containers),
 * then recreates them and re-seeds the potemkin fixtures. record-cli.sh uses
 * this so Scene 2 starts with only the potemkin workspaces present.
 *
 *   devenv shell -- node --experimental-strip-types \
 *       src/frontend/e2e-tests/demo/demo-seed.ts --reset
 *
 * Idempotent: re-running without --reset is safe. Users are created if missing
 * (via the admin endpoint, so this works even if public registration is
 * disabled). Workspaces are find-or-create by name, and role grants are
 * re-applied harmlessly.
 *
 * The admin account is NOT created here — it comes from the server's
 * KLANGKD_DEFAULT_USER. Override anything with env vars: KLANGKBUILD_TEST_URL,
 * KLANGKBUILD_DEMO_PASSWORD, KLANGKBUILD_DEMO_ADMIN_PASSWORD, KLANGKBUILD_DEMO_*_EMAIL.
 */
const DEMO_URL = process.env.KLANGKBUILD_TEST_URL || "http://localhost:8996";

// Bootstrap admin = the server's KLANGKD_DEFAULT_USER. Used for the destructive
// reset and user/group management. MUST differ from the hero (you can't delete
// yourself) and holds the `admin` permission to manage users + groups.
const BOOTSTRAP_EMAIL = process.env.KLANGKD_DEFAULT_USER || "admin@plope.com";
const BOOTSTRAP_PASSWORD = process.env.KLANGKD_DEFAULT_PASSWORD || "admin";

// Hero = the on-camera admin. Created + promoted to the "admin" group by this
// seed, so --reset can fully repave (delete -> recreate) for a clean slate.
const ADMIN_EMAIL =
  process.env.KLANGKBUILD_DEMO_ADMIN_EMAIL || "admin@example.com";
const ADMIN_PASSWORD =
  process.env.KLANGKBUILD_DEMO_ADMIN_PASSWORD || "adminpass";

// Cast users share one password.
const DEMO_PASSWORD = process.env.KLANGKBUILD_DEMO_PASSWORD || "demopass123";

// Virtual users. Each is created via admin (idempotent) and given a personal
// workspace + membership in shared ones below.
const USERS = {
  teammate:
    process.env.KLANGKBUILD_DEMO_TEAMMATE_EMAIL || "teammate@example.com",
  designer:
    process.env.KLANGKBUILD_DEMO_DESIGNER_EMAIL || "designer@example.com",
  reviewer:
    process.env.KLANGKBUILD_DEMO_REVIEWER_EMAIL || "reviewer@example.com",
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
  const r = await post("/auth/login", { identifier: email, password });
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

/** Delete a workspace by name, acting as `token`. No-op if absent. */
async function deleteWorkspace(name: string, token: string): Promise<boolean> {
  const list = await get("/workspaces", token);
  if (!list.ok) return false;
  const items = JSON.parse(list.body);
  const arr = Array.isArray(items) ? items : (items.items ?? []);
  const found = arr.find((w: { name?: string }) => w && w.name === name);
  if (!found) return false;
  const del = await req("DELETE", `/workspaces/${found.id}`, undefined, token);
  return del.ok;
}

/** Find a user id by email (admin token). null if absent. */
async function findUserId(
  email: string,
  adminToken: string,
): Promise<string | null> {
  const r = await get("/admin/users", adminToken);
  if (!r.ok) return null;
  const data = JSON.parse(r.body);
  const arr = Array.isArray(data) ? data : (data.users ?? []);
  const found = arr.find((u: { email?: string }) => u && u.email === email);
  return found ? found.id : null;
}

/** Find a group id by name (admin token). null if absent. */
async function findGroupId(
  name: string,
  adminToken: string,
): Promise<string | null> {
  const r = await get("/admin/groups", adminToken);
  if (!r.ok) return null;
  const data = JSON.parse(r.body);
  const arr = Array.isArray(data) ? data : (data.groups ?? []);
  const found = arr.find((g: { name?: string }) => g && g.name === name);
  return found ? found.id : null;
}

/** Add a user to the "admin" group (idempotent). True if now a member. */
async function ensureAdmin(
  userId: string,
  adminToken: string,
): Promise<boolean> {
  const gid = await findGroupId("admin", adminToken);
  if (!gid) return false;
  const r = await post(
    `/admin/groups/${gid}/members`,
    { user_id: userId },
    adminToken,
  );
  return r.ok || /already|member/i.test(r.body);
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

/** Delete every demo user (hero + cast). Cascades their workspaces +
 *  containers (stop_user_containers) and archives their data. No-op if absent. */
async function resetDemoUsers(bootstrapToken: string): Promise<void> {
  const emails = [ADMIN_EMAIL, ...Object.values(USERS)];
  for (const email of emails) {
    const id = await findUserId(email, bootstrapToken);
    if (id) {
      await req("DELETE", `/admin/users/${id}`, undefined, bootstrapToken);
      console.log(
        `  ✓ deleted user ${email} (cascades workspaces + containers)`,
      );
    } else {
      console.log(`  · absent  user ${email}`);
    }
  }
}

/** Ensure a user exists with the given password; returns id + created flag. */
async function ensureUser(
  email: string,
  password: string,
  adminToken: string,
): Promise<{ created: boolean; id: string }> {
  const existing = await findUserId(email, adminToken);
  if (existing) return { created: false, id: existing };
  const create = await post("/admin/users", { email, password }, adminToken);
  if (!create.ok && !/already|exists/i.test(create.body)) {
    throw new Error(
      `create user ${email} failed (${create.status}): ${create.body.slice(0, 160)}`,
    );
  }
  // POST /admin/users returns {id,...}; fall back to a lookup just in case.
  const id =
    JSON.parse(create.body).id ?? (await findUserId(email, adminToken));
  return { created: true, id };
}

async function seed() {
  const reset = process.argv.includes("--reset");
  console.log(`Demo server: ${DEMO_URL}`);
  console.log(`Hero admin:  ${ADMIN_EMAIL}`);
  if (reset)
    console.log("Reset:       --reset (delete + recreate all demo users)");

  // Bootstrap login: reset + user/group management need the server's admin.
  const bootstrapToken = await login(BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD);
  console.log("  ✓ bootstrap login OK");

  if (reset) {
    // Full repave: deleting the demo users cascades ALL their workspaces
    // (demo, openclaw, AND the potemkin fixtures) + stops their containers
    // + archives their data. Recreated below for a clean recording slate.
    await resetDemoUsers(bootstrapToken);
  } else {
    // Idempotent path (no --reset): just clear the on-camera workspaces so
    // neither survives from a prior recording.
    const heroToken = await login(ADMIN_EMAIL, ADMIN_PASSWORD);
    for (const name of ["demo", "openclaw"]) {
      const removed = await deleteWorkspace(name, heroToken);
      console.log(
        `  ${removed ? "✓ removed" : "· absent  "} workspace "${name}"`,
      );
    }
  }

  // Hero (on-camera admin) — ensure exists + in the "admin" group.
  {
    const { created, id } = await ensureUser(
      ADMIN_EMAIL,
      ADMIN_PASSWORD,
      bootstrapToken,
    );
    console.log(
      `  ${created ? "✓ created" : "✓ exists "} hero: ${ADMIN_EMAIL}`,
    );
    const admin = await ensureAdmin(id, bootstrapToken);
    console.log(`  ${admin ? "✓" : "·"} hero in 'admin' group`);
  }

  // Cast users.
  for (const [label, email] of Object.entries(USERS)) {
    const { created } = await ensureUser(email, DEMO_PASSWORD, bootstrapToken);
    console.log(`  ${created ? "✓ created" : "✓ exists "} ${label}: ${email}`);
  }

  // Tokens for owned-workspace creation.
  const tokens: Record<string, string> = {
    admin: await login(ADMIN_EMAIL, ADMIN_PASSWORD),
  };
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
