import { expect, Page, APIRequestContext } from "@playwright/test";
import { execSync } from "child_process";

// Each test registers its own user and creates its own workspace. This ensures
// tests are fully isolated — logout in one test can't kill another test's
// containers, and parallel execution is safe because no state is shared.

export const BACKEND_PORT = process.env.KLANGK_E2E_PORT || "18997";
export const API_BASE = `http://localhost:${BACKEND_PORT}`;
export const TEST_PASSWORD = "testpass";

/** Register a new user via API (test mode allows unauthenticated registration).
 *  Returns { token, headers }. */
export async function registerUser(
  request: APIRequestContext,
  email: string,
): Promise<{ token: string; headers: Record<string, string> }> {
  const resp = await request.post(`${API_BASE}/auth/register`, {
    data: { email, password: TEST_PASSWORD },
  });
  if (!resp.ok()) {
    const body = await resp.text();
    throw new Error(`Register failed: ${resp.status()} ${body}`);
  }
  const data = await resp.json();
  const token = data.access_token;
  return { token, headers: { Authorization: `Bearer ${token}` } };
}

/** Type email + password into the Flutter login form and click Login.
 *  Returns once the response is received (does not wait for Workspaces). */
export async function loginViaUI(page: Page, email: string, password: string) {
  await page.goto("/");
  await waitForFlutter(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  const f = fv(page);

  // The "Enable accessibility" button (if present) can cover the center of
  // the screen and intercept our login field clicks. Dismiss it first if visible.
  const accessibilityBtn = page.locator("button", {
    hasText: "Enable accessibility",
  });
  if (await accessibilityBtn.isVisible({ timeout: 500 }).catch(() => false)) {
    await accessibilityBtn.click();
    await page.waitForTimeout(300);
  }

  await f.click({ position: { x: cx, y: height * 0.46 }, force: true });
  await page.waitForTimeout(200);
  await page.keyboard.type(email);

  await f.click({ position: { x: cx, y: height * 0.56 }, force: true });
  await page.waitForTimeout(200);
  await page.keyboard.type(password);

  await f.click({ position: { x: cx, y: height * 0.64 }, force: true });
  await expect(page).toHaveTitle(/Workspaces/i, { timeout: 10_000 });
}

/** Like loginViaUI but does not wait for Workspaces — use when
 *  expecting login to fail. Returns the page title after the click. */
export async function tryLogin(page: Page, email: string, password: string) {
  await page.goto("/");
  await waitForFlutter(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  const f = fv(page);

  const accessibilityBtn = page.locator("button", {
    hasText: "Enable accessibility",
  });
  if (await accessibilityBtn.isVisible({ timeout: 500 }).catch(() => false)) {
    await accessibilityBtn.click();
    await page.waitForTimeout(300);
  }

  await f.click({ position: { x: cx, y: height * 0.46 }, force: true });
  await page.waitForTimeout(200);
  await page.keyboard.type(email);

  await f.click({ position: { x: cx, y: height * 0.56 }, force: true });
  await page.waitForTimeout(200);
  await page.keyboard.type(password);

  await f.click({ position: { x: cx, y: height * 0.64 }, force: true });
  await page.waitForTimeout(500);
}

// Flutter Web renders to <canvas> inside <flutter-view>, so standard DOM
// locators (text=, role=, input) don't work. We interact via coordinate
// clicks on <flutter-view> and verify state via page title and screenshots.

export async function waitForFlutter(page: Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 90_000 },
  );
  // Wait for flutter-view to be present and rendered
  await page.waitForSelector("flutter-view", { timeout: 10_000 });
  await page.waitForTimeout(500);
}

export function fv(page: Page) {
  return page.locator("flutter-view");
}

/** Click a position on the Flutter canvas using raw mouse events.
 *  Locator clicks with force:true sometimes don't fire Flutter's tap
 *  recognizer (especially on small targets like IconButtons). Using
 *  page.mouse.move + click sends proper pointer events that Flutter
 *  handles reliably across all browser engines. */
export async function flutterClick(page: Page, x: number, y: number) {
  const box = await fv(page).boundingBox();
  const absX = (box?.x ?? 0) + x;
  const absY = (box?.y ?? 0) + y;
  await page.mouse.move(absX, absY);
  await page.waitForTimeout(200);
  await page.mouse.click(absX, absY);
}

/** Click the logo/back button in the AppBar to navigate back to Workspaces.
 *  WebKit renders the Flutter canvas at a slightly different offset than
 *  Chromium/Firefox, so a single fixed-coordinate click can miss the 36x36
 *  logo target.  This helper tries multiple positions across the logo area
 *  until the page title changes to "Workspaces", or throws after all
 *  attempts are exhausted. */
export async function clickBackToWorkspaces(page: Page, timeout = 30_000) {
  // The logo is a 36x36 widget in the AppBar (~56px tall).  Try a grid
  // of candidate coordinates covering the logo area.  The first hit that
  // triggers navigation wins.
  // Wider grid to handle webkit's larger offset from the Flutter canvas.
  const candidates = [
    { x: 25, y: 28 }, // original center estimate
    { x: 18, y: 28 }, // left
    { x: 32, y: 28 }, // right
    { x: 25, y: 20 }, // higher
    { x: 25, y: 36 }, // lower
    { x: 18, y: 20 }, // top-left
    { x: 32, y: 36 }, // bottom-right
    { x: 40, y: 28 }, // further right
    { x: 12, y: 28 }, // further left
    { x: 25, y: 14 }, // much higher
    { x: 25, y: 44 }, // much lower
    { x: 40, y: 20 }, // top far-right
    { x: 40, y: 36 }, // bottom far-right
  ];

  const deadline = Date.now() + timeout;

  for (const { x, y } of candidates) {
    if (Date.now() >= deadline) break;

    await flutterClick(page, x, y);

    // Give Flutter time to process the tap and start navigation
    try {
      await expect(page).toHaveTitle(/Workspaces/i, {
        timeout: Math.min(3_000, deadline - Date.now()),
      });
      return; // success
    } catch {
      // Title didn't change — try next coordinate
    }
  }

  // Final check with remaining timeout
  await expect(page).toHaveTitle(/Workspaces/i, {
    timeout: Math.max(1_000, deadline - Date.now()),
  });
}

/** Poll the files API until a specific file appears. */
export async function waitForFile(
  request: APIRequestContext,
  workspaceId: string,
  path: string,
  headers: Record<string, string>,
  timeout = 30_000,
) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/files/content?path=${encodeURIComponent(path)}`,
        { headers },
      );
      if (resp.ok()) return;
    } catch {
      // Not ready yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`File ${path} did not appear within ${timeout}ms`);
}

export function vp(page: Page) {
  return page.viewportSize() || { width: 1280, height: 720 };
}

/** Click the terminal area, wait for it to be interactive, then type a command and press Enter. */
export async function terminalType(
  page: Page,
  command: string,
  termX?: number,
  termY?: number,
) {
  const { width, height } = vp(page);
  const x = termX ?? width / 2;
  const y = termY ?? height / 2;
  const f = fv(page);

  await f.click({ position: { x, y }, force: true });
  await page.waitForTimeout(1000);
  await page.keyboard.type(command);
  await page.keyboard.press("Enter");
}

/** Create a workspace via API. Returns workspace ID and cleanup function. */
export async function createWorkspace(
  request: APIRequestContext,
  headers: Record<string, string>,
  namePrefix: string,
): Promise<{
  workspaceId: string;
  cleanup: () => Promise<void>;
}> {
  const name = `${namePrefix}-${Date.now()}@test.example.com`;
  const createResp = await request.post(`${API_BASE}/workspaces`, {
    headers,
    data: { name },
  });
  if (!createResp.ok()) {
    const body = await createResp.text();
    throw new Error(
      `Workspace creation failed: ${createResp.status()} ${body}`,
    );
  }
  const workspace = await createResp.json();
  const workspaceId = workspace.id;

  return {
    workspaceId,
    cleanup: async () => {
      await request.delete(`${API_BASE}/workspaces/${workspaceId}`, {
        headers,
      });
    },
  };
}

/** Open a workspace in the browser and wait for the container to be ready. */
export async function openWorkspace(
  page: Page,
  email: string,
  workspaceId: string,
  { waitForTerminal = false }: { waitForTerminal?: boolean } = {},
) {
  // Set up WebSocket listener before login so we catch all WebSocket
  // connections.  A single framereceived handler watches for both
  // container_ready and (optionally) terminal_started, avoiding the
  // race where a separate waitForTerminalReady listener misses the frame.
  let resolveContainer: () => void;
  let resolveTerminal: (() => void) | null = null;
  const containerPromise = new Promise<void>((resolve, reject) => {
    resolveContainer = resolve;
    setTimeout(
      () => reject(new Error("Container did not become ready within 120s")),
      120_000,
    );
  });
  const terminalPromise = waitForTerminal
    ? new Promise<void>((resolve, reject) => {
        resolveTerminal = resolve;
        setTimeout(
          () => reject(new Error("Terminal did not become ready within 120s")),
          120_000,
        );
      })
    : Promise.resolve();

  page.on("websocket", (ws: { on: Function }) => {
    ws.on("framereceived", (frame: { payload: string | Buffer }) => {
      const text = frame.payload.toString();
      if (text.includes("container_ready")) resolveContainer!();
      if (resolveTerminal && text.includes("terminal_started"))
        resolveTerminal();
    });
  });

  await loginViaUI(page, email, TEST_PASSWORD);
  // Navigate to the workspace via goto() with the full URL including hash.
  // Using goto() tears down the old page atomically and loads a fresh one
  // with the hash already set. This avoids a race where setting the hash
  // first triggers Flutter to start a container, then reload() kills that
  // connection, and the second attempt gets a 409 container name conflict.
  await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
  await waitForFlutter(page);
  await containerPromise;
  await terminalPromise;
}

/** Convenience: register user, create workspace, open it. */
export async function createAndOpenWorkspace(
  page: Page,
  request: APIRequestContext,
  namePrefix: string,
  { waitForTerminal = false }: { waitForTerminal?: boolean } = {},
): Promise<{
  workspaceId: string;
  email: string;
  token: string;
  headers: Record<string, string>;
  cleanup: () => Promise<void>;
}> {
  const email = `${namePrefix}-${Date.now()}@test.example.com`;
  const { token, headers } = await registerUser(request, email);
  const { workspaceId, cleanup } = await createWorkspace(
    request,
    headers,
    namePrefix,
  );
  await openWorkspace(page, email, workspaceId, { waitForTerminal });
  return { workspaceId, email, token, headers, cleanup };
}

/** Environment for podman subprocesses: strips LD_LIBRARY_PATH so
 *  the nix-installed binary uses RPATH and system binaries don't
 *  pick up nix's glibc. */
function podmanEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  delete env.LD_LIBRARY_PATH;
  return env;
}

export function dockerContainersForWorkspace(workspaceId: string): string[] {
  const podman = process.env.KLANGK_PODMAN_BIN || "podman";
  const output = execSync(
    `${podman} ps --filter "label=klangk.workspace-id=${workspaceId}" --format "{{.ID}}"`,
    { encoding: "utf-8", env: podmanEnv() },
  );
  return output.trim().split("\n").filter(Boolean);
}

// Layout coordinates at 1280x720:
// Terminal/Files panel: full width (x 0-1280)
// Tab bar (Terminal/Files): y ~0-32
// Back button: x ~25, y ~28
