import { expect, Page, APIRequestContext } from "@playwright/test";
import { execSync } from "child_process";

// Each test registers its own user and creates its own workspace. This ensures
// tests are fully isolated — logout in one test can't kill another test's
// containers, and parallel execution is safe because no state is shared.

export const BACKEND_PORT = process.env.BARK_E2E_PORT || "18997";
export const API_BASE = `http://localhost:${BACKEND_PORT}`;
export const TEST_PASSWORD = "testpass";

/** Register a new user via API (test mode allows unauthenticated registration).
 *  Returns { token, headers }. */
export async function registerUser(
  request: APIRequestContext,
  username: string,
): Promise<{ token: string; headers: Record<string, string> }> {
  const resp = await request.post(`${API_BASE}/auth/register`, {
    data: { username, password: TEST_PASSWORD },
  });
  if (!resp.ok()) {
    const body = await resp.text();
    throw new Error(`Register failed: ${resp.status()} ${body}`);
  }
  const data = await resp.json();
  const token = data.access_token;
  return { token, headers: { Authorization: `Bearer ${token}` } };
}

/** Log in via the UI by typing credentials into the Flutter login form. */
export async function loginViaUI(
  page: Page,
  username: string,
  password: string,
) {
  await page.goto("/");
  await waitForFlutter(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  const f = fv(page);

  await f.click({ position: { x: cx, y: height * 0.47 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(username);

  await f.click({ position: { x: cx, y: height * 0.55 }, force: true });
  await page.waitForTimeout(300);
  await page.keyboard.type(password);

  await f.click({ position: { x: cx, y: height * 0.66 }, force: true });
  await expect(page).toHaveTitle(/Workspaces/i, { timeout: 15_000 });
}

// Flutter Web renders to <canvas> inside <flutter-view>, so standard DOM
// locators (text=, role=, input) don't work. We interact via coordinate
// clicks on <flutter-view> and verify state via page title and screenshots.

export async function waitForFlutter(page: Page) {
  await page.waitForFunction(
    () => !document.body.textContent?.includes("Loading, please wait"),
    { timeout: 90_000 },
  );
  await page.waitForTimeout(1000);
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

/** Type a prompt into the chat input and verify it was received by the backend.
 *  On slower environments (CI, WebKit), the Flutter chat widget may not be
 *  fully wired up when container_ready fires. This function retries once
 *  if the user message doesn't appear in the backend within 10s. */
export async function sendPrompt(
  page: Page,
  request: APIRequestContext,
  workspaceId: string,
  headers: Record<string, string>,
  text: string,
) {
  const { height } = vp(page);

  const typeAndSend = async () => {
    await flutterClick(page, 240, height - 30);
    await page.waitForTimeout(500);
    // Select all + delete to clear any leftover text from a failed attempt
    await page.keyboard.press("Control+a");
    await page.keyboard.press("Backspace");
    await page.waitForTimeout(200);
    await page.keyboard.type(text);
    await page.waitForTimeout(300);
    await page.keyboard.press("Enter");
  };

  const checkReceived = async (): Promise<boolean> => {
    for (let i = 0; i < 5; i++) {
      await page.waitForTimeout(1000);
      const msgResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/messages`,
        { headers },
      );
      if (msgResp.ok()) {
        const messages = await msgResp.json();
        if (messages.some((m: any) => m.entry_type === "user")) return true;
      }
    }
    return false;
  };

  await typeAndSend();
  if (await checkReceived()) return;
  // Retry once — the chat widget may not have been ready
  await typeAndSend();
  if (await checkReceived()) return;
  throw new Error(
    `Prompt "${text}" was not received by the backend after 2 attempts`,
  );
}

/** Click the terminal area, wait for it to be interactive, then type a command and press Enter. */
export async function terminalType(
  page: Page,
  command: string,
  termX?: number,
  termY?: number,
) {
  const { width, height } = vp(page);
  const x = termX ?? (492 + width) / 2;
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
  const name = `${namePrefix}-${Date.now()}`;
  const createResp = await request.post(
    `${API_BASE}/workspaces?name=${encodeURIComponent(name)}`,
    { headers },
  );
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
  username: string,
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

  await loginViaUI(page, username, TEST_PASSWORD);
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
  username: string;
  token: string;
  headers: Record<string, string>;
  cleanup: () => Promise<void>;
}> {
  const username = `${namePrefix}-${Date.now()}`;
  const { token, headers } = await registerUser(request, username);
  const { workspaceId, cleanup } = await createWorkspace(
    request,
    headers,
    namePrefix,
  );
  await openWorkspace(page, username, workspaceId);
  return { workspaceId, username, token, headers, cleanup };
}

export function dockerContainersForWorkspace(workspaceId: string): string[] {
  const output = execSync(
    `docker ps --filter "label=bark.workspace-id=${workspaceId}" --format "{{.ID}}"`,
    { encoding: "utf-8" },
  );
  return output.trim().split("\n").filter(Boolean);
}

// Layout coordinates at 1280x720:
// Chat panel: x 0-486 (38%)
// Right panel: x 492-1280
// Tab bar (Terminal/Files): y ~0-32 in right panel
// Chat input: bottom of left panel, ~y 690
// Debug bar: bottom of right panel
// Back button: x ~25, y ~28
