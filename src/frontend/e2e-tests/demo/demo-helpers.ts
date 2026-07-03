/**
 * Demo helpers for the Klangk intro video.
 *
 * These build on the *existing* e2e helper primitives in
 * `../e2e/helpers.ts` (which already solve the hard part: Flutter Web renders
 * to <canvas> inside <flutter-view>, so all interaction is coordinate-based
 * clicks + keyboard typing). We re-export those primitives and add:
 *
 *   - pacing (`pace`) and human-feel typing (`slowType`) for narration room,
 *   - real-password login (`demoLogin`) — the e2e helpers hardcode a test pw,
 *   - API login / register / admin-create-user, so scenes can run against a
 *     REAL demo server (port 8996, real LLM key) via KLANGK_TEST_URL,
 *   - tab openers for the 5 workspace tabs,
 *   - a tiny WS client for reliable chat-send / terminal-share when on-screen
 *     typing is flaky.
 *
 * Run against an already-running demo server (the README explains how).
 */
import { Page, APIRequestContext } from "@playwright/test";
import {
  waitForFlutter,
  fv,
  vp,
  flutterClick,
  clickBackToWorkspaces,
  openFilesTab,
  clickFileRow,
  terminalType,
  seedFile,
} from "../e2e/helpers";
import WebSocket from "ws";

/** The demo server. Default is the real klangk port; override with KLANGK_TEST_URL. */
export const DEMO_URL = process.env.KLANGK_TEST_URL || "http://localhost:8996";

/** Password for freshly-registered demo accounts (scenes self-register). */
export const DEMO_PASSWORD = process.env.KLANGK_DEMO_PASSWORD || "demopass123";

/** Seeded admin credentials. Falls back to KLANGK_DEFAULT_USER so the demo
 *  picks up whatever admin the server actually seeded. Override with
 *  KLANGK_DEMO_ADMIN_EMAIL / KLANGK_DEMO_ADMIN_PASSWORD. */
export const DEMO_ADMIN_EMAIL =
  process.env.KLANGK_DEMO_ADMIN_EMAIL ||
  process.env.KLANGK_DEFAULT_USER ||
  "admin@example.com";
export const DEMO_ADMIN_PASSWORD =
  process.env.KLANGK_DEMO_ADMIN_PASSWORD ||
  process.env.KLANGK_DEFAULT_PASSWORD ||
  "admin";

// Re-export the password-agnostic Flutter primitives scenes rely on.
export {
  waitForFlutter,
  fv,
  vp,
  flutterClick,
  clickBackToWorkspaces,
  openFilesTab,
  clickFileRow,
  terminalType,
  seedFile,
};

// ---------------------------------------------------------------------------
// Pacing / typing
// ---------------------------------------------------------------------------

/** Sleep for ms milliseconds. Use to leave narration room in a take. */
export function pace(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Type text character-by-character into whatever field is focused, for a
 *  human "typed" look. Does NOT press Enter. Call `pace`/Enter separately.
 *  Default ~12 chars/sec with light jitter. */
export async function slowType(
  page: Page,
  text: string,
  { cps = 12, jitter = 25 }: { cps?: number; jitter?: number } = {},
) {
  const perChar = 1000 / cps;
  for (const ch of text) {
    await page.keyboard.type(ch);
    const wobble = jitter ? (Math.random() * 2 - 1) * jitter : 0;
    await pace(Math.max(0, perChar + wobble));
  }
}

// ---------------------------------------------------------------------------
// Visible-cursor overlay (the "frozen cursor" fix)
// ---------------------------------------------------------------------------
//
// `page.mouse.move/click` dispatch synthetic CDP pointer events that Flutter
// handles fine, but they move NO real OS cursor — so in the Xvfb recording the
// pointer appears frozen even though the clicks land. The proven, framework-
// agnostic fix (used by vercel-labs/open-agents, pagecast, amux, Assrt) is a
// DOM overlay: a high-z-index, pointer-events:none SVG arrow that repositions
// on every DOM `mousemove` (which synthetic CDP events DO fire) plus a ripple
// on `mousedown`. Because it is a real DOM node it renders above Flutter's
// <canvas> and is captured by ffmpeg natively — no CDP screencast needed.
const CURSOR_INJECT_SCRIPT = `
(function () {
  if (window.__klangkCursor) return;
  window.__klangkCursor = true;

  // Paint the page background the app's own dark charcoal (#1e1e1e ≈ Y30) so any
  // area Flutter doesn't fill (a viewport/crop height mismatch under Xvfb) shows
  // the app color instead of the browser's default WHITE canvas — invisible,
  // not a glaring white bar. NOTE: this MUST be deferred (called from watchBody
  // below, never synchronously at init) — at init-script time documentElement
  // may be null, and touching it then throws and aborts the whole IIFE, killing
  // the cursor overlay too. Same deferral pattern as the SVG mount() below.
  function paintBg() {
    var de = document.documentElement;
    if (de) de.style.background = "#1e1e1e";
    if (document.body) document.body.style.background = "#1e1e1e";
  }
  (function watchBody() {
    if (!document.body) { setTimeout(watchBody, 20); return; }
    paintBg();
  })();
  var NS = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(NS, "svg");
  // Classic arrow pointer, tip at the SVG origin so translate(clientX,Y)
  // lands the tip on the actual pointer location.
  svg.setAttribute("viewBox", "0 0 14 22");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "31");
  svg.style.cssText =
    "position:fixed;left:0;top:0;margin:0;padding:0;" +
    "pointer-events:none;z-index:2147483647;opacity:0;" +
    "transition:opacity .15s ease;will-change:transform;" +
    "filter:drop-shadow(0 1px 2px rgba(0,0,0,.55));";
  var path = document.createElementNS(NS, "path");
  path.setAttribute(
    "d",
    "M0,0 L0,17 L4.5,13 L7.5,19 L9.5,18 L6.5,12 L11,12 Z",
  );
  path.setAttribute("fill", "#ffffff");
  path.setAttribute("stroke", "#111111");
  path.setAttribute("stroke-width", "1.4");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);

  function ensureRoot() {
    // Append (or re-append if Flutter swapped the body) so the cursor is
    // never lost after a late render. Called from mount + every move.
    if (!svg.isConnected && document.body) document.body.appendChild(svg);
  }
  (function mount() {
    if (document.body) ensureRoot();
    else setTimeout(mount, 30);
  })();

  window.addEventListener(
    "mousemove",
    function (e) {
      ensureRoot();
      svg.style.transform =
        "translate(" + e.clientX + "px," + e.clientY + "px)";
      svg.style.opacity = "1";
    },
    { passive: true },
  );

  // Click ripple — injected style is created once on first click.
  window.addEventListener(
    "mousedown",
    function (e) {
      ensureRoot();
      if (!document.getElementById("klangk-ripple-kf")) {
        var st = document.createElement("style");
        st.id = "klangk-ripple-kf";
        st.textContent =
          "@keyframes klangkRipple{0%{transform:scale(.5);opacity:.9}" +
          "100%{transform:scale(2.4);opacity:0}}";
        (document.head || document.documentElement).appendChild(st);
      }
      var r = document.createElement("div");
      r.style.cssText =
        "position:fixed;left:" + e.clientX + "px;top:" + e.clientY +
        "px;width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;" +
        "border:2px solid rgba(86,156,214,.95);pointer-events:none;" +
        "z-index:2147483646;animation:klangkRipple .5s ease-out forwards";
      document.body.appendChild(r);
      setTimeout(function () { r.remove(); }, 520);
    },
    { passive: true },
  );
})();
`;

/** Install the fake-cursor overlay on the next (and every) navigation.
 *  Idempotent (the script self-guards). No-op when KLANGK_DEMO_CURSOR=0 (e.g.
 *  a quick headless dry check where the overlay is unwanted). */
export async function installDemoCursor(page: Page) {
  if (process.env.KLANGK_DEMO_CURSOR === "0") return;
  await page.addInitScript(CURSOR_INJECT_SCRIPT);
}

// ---------------------------------------------------------------------------
// Auth (against the real demo server)
// ---------------------------------------------------------------------------

async function postJson(
  request: APIRequestContext,
  url: string,
  data: Record<string, unknown>,
  headers: Record<string, string> = {},
) {
  const resp = await request.post(url, { data, headers, timeout: 30_000 });
  if (!resp.ok()) {
    throw new Error(
      `${url} failed: ${resp.status()} ${await resp.text().catch(() => "")}`,
    );
  }
  return resp.json();
}

/** Register a fresh demo user (requires registration enabled on the server).
 *  Returns { token, headers }. */
export async function registerDemoUser(
  request: APIRequestContext,
  email: string,
  password = DEMO_PASSWORD,
) {
  const d = await postJson(request, `${DEMO_URL}/api/v1/auth/register`, {
    email,
    password,
  });
  const token = d.access_token;
  return { token, headers: { Authorization: `Bearer ${token}` } };
}

/** Log in via the API. Returns { token, headers, id }. */
export async function apiLogin(
  request: APIRequestContext,
  email: string,
  password = DEMO_PASSWORD,
) {
  const d = await postJson(request, `${DEMO_URL}/api/v1/auth/login`, {
    email,
    password,
  });
  const token = d.access_token;
  return { token, headers: { Authorization: `Bearer ${token}` } };
}

/** Log in as the seeded admin. */
export async function adminLogin(request: APIRequestContext) {
  return apiLogin(request, DEMO_ADMIN_EMAIL, DEMO_ADMIN_PASSWORD);
}

/** Idempotently ensure a user exists. Tries login first; if that fails, creates
 *  the user via the admin endpoint (works even when registration is disabled).
 *  Returns { token, headers } or null if the user can't be established. */
export async function ensureUser(
  request: APIRequestContext,
  email: string,
  password = DEMO_PASSWORD,
) {
  try {
    return await apiLogin(request, email, password);
  } catch {
    // not present / wrong pw — try to create via admin
  }
  const admin = await adminLogin(request);
  await postJson(
    request,
    `${DEMO_URL}/api/v1/admin/users`,
    {
      email,
      password,
    },
    admin.headers,
  );
  return apiLogin(request, email, password);
}

/** Create a workspace via API. Returns the workspace object. */
export async function createWorkspace(
  request: APIRequestContext,
  headers: Record<string, string>,
  name: string,
) {
  return postJson(request, `${DEMO_URL}/api/v1/workspaces`, { name }, headers);
}

/** Find a workspace owned by the caller by name (GET /workspaces returns a bare
 *  list when called without limit/offset). Returns the ws object or undefined. */
export async function findWorkspaceByName(
  request: APIRequestContext,
  headers: Record<string, string>,
  name: string,
) {
  const resp = await request.get(`${DEMO_URL}/api/v1/workspaces`, {
    headers,
    timeout: 30_000,
  });
  if (!resp.ok()) {
    throw new Error(`list workspaces failed: ${resp.status()}`);
  }
  const items = (await resp.json()) as Array<Record<string, unknown>>;
  return items.find((w) => w.name === name);
}

/** Delete a workspace by id (owner only). Best-effort (ignores 404). */
export async function deleteWorkspace(
  request: APIRequestContext,
  headers: Record<string, string>,
  id: string,
) {
  await request.delete(`${DEMO_URL}/api/v1/workspaces/${id}`, {
    headers,
    timeout: 30_000,
  });
}

/** Idempotent re-record helper: ensure a workspace named *name* is freshly
 *  created (any existing one is deleted first) so each take starts clean while
 *  keeping a STABLE name for on-screen continuity. Returns the new workspace. */
export async function ensureFreshWorkspace(
  request: APIRequestContext,
  headers: Record<string, string>,
  name: string,
  extra: Record<string, unknown> = {},
) {
  const existing = await findWorkspaceByName(request, headers, name);
  if (existing) await deleteWorkspace(request, headers, existing.id as string);
  return postJson(
    request,
    `${DEMO_URL}/api/v1/workspaces`,
    { name, ...extra },
    headers,
  );
}

/** Add a user to a workspace role. role ∈ owners|coders|collaborators|spectators. */
export async function addRole(
  request: APIRequestContext,
  headers: Record<string, string>,
  workspaceId: string,
  role: "owners" | "coders" | "collaborators" | "spectators",
  email: string,
) {
  await request.post(
    `${DEMO_URL}/api/v1/workspaces/${workspaceId}/roles/${role}`,
    { data: { email }, headers, timeout: 30_000 },
  );
}

// ---------------------------------------------------------------------------
// UI login + workspace open (real passwords, not the e2e test password)
// ---------------------------------------------------------------------------

/** Dismiss the Flutter "Enable accessibility" overlay if present. */
async function dismissAccessibility(page: Page) {
  const btn = page.locator("button", { hasText: "Enable accessibility" });
  if (await btn.isVisible({ timeout: 500 }).catch(() => false)) {
    await btn.click();
    await pace(300);
  }
}

/** Glide the OS mouse cursor to a point on the Flutter canvas and click it.
 *
 *  Uses absolute `page.mouse` events (not a locator click), so the cursor is
 *  VISIBLE in the recording and animates smoothly to the target across
 *  `steps` sub-moves — viewers can follow where the action happens.
 *  Coordinates are relative to <flutter-view>'s top-left, matching the rest
 *  of the helpers.
 *
 *  Prefer this over locator/coordinate clicks for any on-camera navigation
 *  (tabs, buttons, workspaces, login). `page.mouse` sends real pointer events
 *  that Flutter handles reliably across browsers (more so than force:true
 *  locator clicks on small targets). */
export async function mouseClick(
  page: Page,
  x: number,
  y: number,
  { steps = 25, settleMs = 150 }: { steps?: number; settleMs?: number } = {},
) {
  const box = await fv(page).boundingBox();
  const absX = (box?.x ?? 0) + x;
  const absY = (box?.y ?? 0) + y;
  await page.mouse.move(absX, absY, { steps });
  await page.waitForTimeout(settleMs);
  await page.mouse.click(absX, absY);
}

/** Log in via the Flutter login form using coordinate clicks on the canvas.
 *  CRITICAL: do NOT enable Flutter semantics here. Semantics-on interferes
 *  with the terminal xterm widget's FocusNode, so typing into the terminal
 *  silently fails later in the scene. The real e2e suite likewise keeps a
 *  clean canvas and types via coordinate clicks.
 *
 *  Coords are fractional Y of the login card in `both` auth mode (the demo
 *  server runs KLANGK_AUTH_MODES=both, so the "Log in with Enfold" OIDC
 *  button sits above the fields and shifts them down ~0.07 vs password-only).
 *  Measured from the semantics-DOM bounding boxes at 1920×1080 logical and
 *  confirmed working (click → spinner → Workspaces) at this viewport:
 *    Email   ~0.529   Password ~0.588   Log In button ~0.644
 *  Pure fractions of viewport height. NOTE: these are 1080-logical values;
 *  the login card sits at different fractions at other heights, so re-measure
 *  if the viewport changes. (And: do NOT switch to a <1080 viewport with
 *  deviceScaleFactor to scale up — DPR>1 breaks Flutter Web button taps.) */
export async function demoLogin(
  page: Page,
  email: string,
  password = DEMO_PASSWORD,
) {
  // Inject the fake cursor BEFORE the first navigation so it is in place
  // when the Flutter app renders. See installDemoCursor().
  await installDemoCursor(page);
  await page.goto("/");
  await waitForFlutter(page);
  // Dismiss the "Enable accessibility" overlay if visible — it can cover the
  // canvas and intercept clicks (same defensive step the e2e suite takes).
  await dismissAccessibility(page);

  const { width, height } = vp(page);
  const cx = width / 2;
  // The login card's vertical fractions depend on viewport height (it doesn't
  // scale linearly — it sits lower at short heights). Measured at both sizes:
  //   1080-tall: email .529  pw .588  btn .644
  //    540-tall: email .540  pw .651  btn .766
  const fyEmail = height <= 600 ? 0.54 : 0.529;
  const fyPw = height <= 600 ? 0.651 : 0.588;
  const fyBtn = height <= 600 ? 0.78 : 0.644;

  // Click each field with the visible mouse cursor (animated glide), then
  // type via the keyboard — credentials still have to be typed, but every
  // click the viewer sees is a real cursor movement, not a teleport.
  await mouseClick(page, cx, height * fyEmail); // Email field
  await page.keyboard.type(email);

  await mouseClick(page, cx, height * fyPw); // Password field
  await page.keyboard.type(password);

  // Submit by clicking the "Log In" button with the cursor (not Enter).
  await mouseClick(page, cx, height * fyBtn);

  // Wait for the Workspaces page to load.
  await page.waitForFunction(() => /Workspaces/i.test(document.title), {
    timeout: 15_000,
  });
  await dismissAccessibility(page);
}

/** Open a workspace by URL hash and wait for its container to be ready.
 *
 *  By default waits for a `container_ready` WebSocket frame (first open, when
 *  the container boots). Pass `waitForContainer: false` when re-opening a
 *  workspace whose container is ALREADY running — a running container never
 *  re-emits `container_ready`, so the wait would hang until `containerTimeout`.
 *
 *  Pass `waitForTerminal: true` to additionally wait for `terminal_started`
 *  — the terminal's FocusNode isn't wired up until that frame, so typing
 *  before it is silently dropped. Required for any scene that types into the
 *  terminal. */
export async function openWorkspaceDemo(
  page: Page,
  email: string,
  workspaceId: string,
  password = DEMO_PASSWORD,
  {
    containerTimeout = 120_000,
    waitForContainer = true,
    waitForTerminal = false,
    holdOnListMs = 0,
  }: {
    containerTimeout?: number;
    waitForContainer?: boolean;
    waitForTerminal?: boolean;
    holdOnListMs?: number;
  } = {},
) {
  let resolveReady: (() => void) | null = null;
  let resolveTerminal: (() => void) | null = null;
  const secs = Math.round(containerTimeout / 1000);
  // Only create the container/terminal promises when we actually wait for
  // them — an unawaited promise whose setTimeout rejects would surface as an
  // unhandled rejection and fail the test (the re-open / no-wait path).
  const ready = waitForContainer
    ? new Promise<void>((resolve, reject) => {
        resolveReady = resolve;
        setTimeout(
          () => reject(new Error("Container not ready within timeout")),
          containerTimeout,
        );
      })
    : Promise.resolve();
  const terminalReady = waitForTerminal
    ? new Promise<void>((resolve, reject) => {
        resolveTerminal = resolve;
        setTimeout(
          () => reject(new Error(`Terminal did not start within ${secs}s`)),
          containerTimeout,
        );
      })
    : Promise.resolve();
  if (waitForContainer || waitForTerminal) {
    page.on("websocket", (ws) => {
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (resolveReady && text.includes("container_ready")) resolveReady();
        if (resolveTerminal && text.includes("terminal_started"))
          resolveTerminal();
      });
    });
  }

  await demoLogin(page, email, password);
  // Optionally hold on the Workspaces list (the post-login landing page) so
  // the viewer sees the workspace card before we open it — without a second
  // login (which would race terminal_started on the WS).
  if (holdOnListMs) await pace(holdOnListMs);
  await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
  await waitForFlutter(page);
  if (waitForContainer) await ready;
  await terminalReady;
  await dismissAccessibility(page);
  await pace(800);
}

// ---------------------------------------------------------------------------
// Workspace navigation — semantics-label clicks (robust)
// ---------------------------------------------------------------------------
// Workspace navigation — coordinate + visible-mouse clicks.
//
// We keep Flutter accessibility OFF (it breaks terminal typing — see the
// mouseClick/demoLogin docs), so navigation can't use the semantics DOM.
// Instead we click measured viewport-fraction coordinates with the visible
// mouse cursor.
//
// Tab positions are VIEWPORT-HEIGHT-DEPENDENT (the header/tab strip doesn't
// scale linearly), so fractions are picked per height — same approach as
// demoLogin. Measured by pixel analysis of the rendered workspace at each size:
//   1080-tall: tab strip fracY ≈ 0.0704   (was the original, all-1080 value)
//    540-tall: tab strip fracY ≈ 0.139    (tabs sit much lower proportionally)
// Tabs are evenly spaced horizontally: fracX = (index + 0.5) / 5 (verified at
// both sizes). The "+" (new-terminal) button sits at the far left of the tab
// strip: fracX ≈ 0.05, fracY = the tab fracY.
function tabFracY(height: number): number {
  return height <= 600 ? 0.139 : 0.0704;
}

/** Click a workspace nav tab by 0-based index (0=Terminal … 4=Settings) using
 *  the visible mouse cursor. */
export async function openTab(page: Page, index: number) {
  const { width, height } = vp(page);
  await mouseClick(
    page,
    width * ((index + 0.5) / 5),
    height * tabFracY(height),
  );
  await pace(350);
}

export const openChatTab = (page: Page) => openTab(page, 2);
export const openSharingTab = (page: Page) => openTab(page, 3);
export const openSettingsTab = (page: Page) => openTab(page, 4);

/** Seed a file into a workspace's home via the demo server's upload API.
 *  (The re-exported e2e `seedFile` hardcodes the e2e test port 18997, so this
 *  demo-specific version uses DEMO_URL.) */
export async function seedDemoFile(
  request: APIRequestContext,
  workspaceId: string,
  path: string,
  content: string | Buffer,
  headers: Record<string, string>,
  mimeType = "application/octet-stream",
) {
  const name = path.split("/").pop()!;
  const buffer = typeof content === "string" ? Buffer.from(content) : content;
  const resp = await request.post(
    `${DEMO_URL}/api/v1/workspaces/${workspaceId}/files/upload?path=${encodeURIComponent(path)}`,
    { headers, multipart: { file: { name, mimeType, buffer } } },
  );
  if (!resp.ok()) {
    throw new Error(
      `seedDemoFile ${path} failed: ${resp.status()} ${await resp.text()}`,
    );
  }
}

/** Add a new terminal tab by clicking the "+" ("New terminal") button in the
 *  terminal tab strip with the visible mouse cursor. */
export const addTerminalTab = (page: Page) => {
  const { width, height } = vp(page);
  return mouseClick(page, width * 0.05, height * tabFracY(height));
};

/** Wait until the workspace terminal is interactive. Semantics-independent:
 *  listens for the `terminal_started` WebSocket frame (the terminal's
 *  FocusNode isn't wired until then, so typing before it is dropped). For a
 *  freshly-opened workspace the container boots a few seconds after
 *  navigation, so the frame arrives after this listener attaches. Resolves
 *  on timeout too (best-effort) so a missed frame never breaks a scene. */
export async function waitForTerminal(
  page: Page,
  { timeout = 30_000 }: { timeout?: number } = {},
) {
  await new Promise<void>((resolve) => {
    const to = setTimeout(resolve, timeout);
    page.on("websocket", (ws) =>
      ws.on("framereceived", (f: { payload: string | Buffer }) => {
        if (f.payload.toString().includes("terminal_started")) {
          clearTimeout(to);
          resolve();
        }
      }),
    );
  });
  await pace(500);
}

// ---------------------------------------------------------------------------
// Tiny WS client (reliable fallback for chat-send / terminal-share when
// on-screen typing/clicking is flaky). Mirrors docs-*-screenshots specs.
// ---------------------------------------------------------------------------

interface WsMessage {
  type?: string;
  [k: string]: unknown;
}

export class DemoWs {
  private ws: WebSocket;
  private queue: WsMessage[] = [];
  private waiters: Array<(m: WsMessage) => void> = [];
  ready = false;

  constructor(ws: WebSocket) {
    this.ws = ws;
    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());
      if (msg.type === "container_ready")
        ws.send(JSON.stringify({ cmd: "ui_ready" }));
      if (msg.type === "event" && msg.event?.name === "container_ready")
        this.ready = true;
      if (this.waiters.length) this.waiters.shift()!(msg);
      else this.queue.push(msg);
    });
  }

  send(msg: Record<string, unknown>) {
    this.ws.send(JSON.stringify(msg));
  }

  async recv(timeout = 30_000): Promise<WsMessage> {
    if (this.queue.length) return this.queue.shift()!;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("recv timed out")), timeout);
      this.waiters.push((m) => {
        clearTimeout(t);
        resolve(m);
      });
    });
  }

  async recvUntil(
    pred: (m: WsMessage) => boolean,
    timeout = 60_000,
  ): Promise<WsMessage> {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const m = await this.recv(deadline - Date.now());
      if (pred(m)) return m;
    }
    throw new Error("recvUntil timed out");
  }

  close() {
    this.ws.close();
  }
}

/** Connect a WS, perform the workspace_connect handshake, return a ready client. */
export async function connectWs(
  token: string,
  workspaceId: string,
): Promise<DemoWs> {
  const wsUrl = DEMO_URL.replace(/^http/, "ws");
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const client = new DemoWs(ws);
    const t = setTimeout(() => {
      ws.close();
      reject(new Error("ws connect timed out"));
    }, 90_000);
    ws.on("open", () => client.send({ cmd: "workspace_connect", workspaceId }));
    ws.on("error", (err) => {
      clearTimeout(t);
      reject(err);
    });
    (async () => {
      await client.recvUntil((m) => m.type === "container_ready");
      client.send({ cmd: "ui_ready" });
      await client.recvUntil(
        (m) =>
          m.type === "event" &&
          (m.event as Record<string, unknown>)?.name === "container_ready",
      );
      clearTimeout(t);
      resolve(client);
    })().catch((e) => {
      clearTimeout(t);
      reject(e);
    });
  });
}

/** Send a chat message over WS and return once it is broadcast back. */
export async function sendChatViaWs(
  token: string,
  workspaceId: string,
  message: string,
) {
  const ws = await connectWs(token, workspaceId);
  ws.send({ cmd: "chat_send", message });
  await ws.recvUntil(
    (m) =>
      m.type === "chat_message" &&
      (m as { message?: string }).message === message,
  );
  return ws;
}
