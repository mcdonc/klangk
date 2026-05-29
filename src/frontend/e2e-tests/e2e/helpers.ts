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
  if (await accessibilityBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
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
  if (await accessibilityBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
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
) {
  // Set up WebSocket listener before login so we catch all WebSocket connections
  const readyPromise = new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error("Container did not become ready within 120s")),
      120_000,
    );
    const listenForReady = (ws: { on: Function }) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        if (frame.payload.toString().includes("container_ready")) {
          clearTimeout(timeout);
          resolve();
        }
      });
    };
    // Listen on any new WebSocket connections
    page.on("websocket", listenForReady);
  });

  await loginViaUI(page, email, TEST_PASSWORD);
  // Use full URL (not just #fragment) so the page reloads and creates a new
  // WebSocket — a hash-only change is handled internally by Flutter's router
  // without opening a new WebSocket, so our listener would never fire.
  await page.goto(`/#/workspace/${workspaceId}`);
  await readyPromise;

  // Extra settle time for the UI to render after container ready.
  // WebKit on CI needs more time for Flutter to fully process the
  // container_ready event and establish bidirectional chat.
  // await page.waitForTimeout(4000);
}

/** Convenience: register user, create workspace, open it. */
export async function createAndOpenWorkspace(
  page: Page,
  request: APIRequestContext,
  namePrefix: string,
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
  await openWorkspace(page, email, workspaceId);
  return { workspaceId, email, token, headers, cleanup };
}

export function dockerContainersForWorkspace(workspaceId: string): string[] {
  const output = execSync(
    `docker ps --filter "label=klangk.workspace-id=${workspaceId}" --format "{{.ID}}"`,
    { encoding: "utf-8" },
  );
  return output.trim().split("\n").filter(Boolean);
}

// Layout coordinates at 1280x720:
// Terminal/Files panel: full width (x 0-1280)
// Tab bar (Terminal/Files): y ~0-32
// Back button: x ~25, y ~28
