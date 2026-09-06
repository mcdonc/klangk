import { test, expect, Page } from "@playwright/test";
import {
  API_BASE,
  createAndOpenWorkspace,
  seedFile,
  openFilesTab,
  clickFileRow,
  terminalType,
} from "./helpers";

// #3219 — runtime CSP / Trusted Types enforcement. The served policy drops
// 'unsafe-inline' from script-src (index.html's inline scripts ride SHA-256
// hash tokens) and adds require-trusted-types-for 'script'. This spec drives
// the acceptance flows (login, workspace page, terminal, file upload, file
// view, download, PDF viewer) and asserts the browser console stays clean of
// CSP and Trusted Types violations.
//
// #3228 — fully self-contained frontend. The engine's CanvasKit renderer
// used to lazily fetch per-script Noto fallback fonts from
// fonts.gstatic.com whenever a frame rasterized a codepoint the bundled
// fonts lack; the whole set is now vendored under
// assets/assets/fallback-fonts/ and web/flutter_bootstrap.js points the
// engine's fontFallbackBaseUrl there, so fallbacks resolve same-origin.
// The known-noise filter that #3219 carried for those blocked fetches is
// gone: any external-origin request — attempted or successful, for fonts,
// JS, CSS, or images — fails the suite, and the terminal flow renders CJK
// output so the fallback chain provably fires during the run.

function watchViolations(page: Page, sink: string[]) {
  // "Trusted" covers Trusted Types, TrustedScriptURL, and TrustedHTML —
  // Chromium's TT sink message is "This document requires
  // 'TrustedScriptURL' assignment. The action has been blocked.", which
  // none of the older alternatives match.
  const pattern =
    /Content Security Policy|Refused to|Trusted|trustedTypes|violates|CSP/;
  page.on("console", (msg) => {
    if (msg.type() === "error" && pattern.test(msg.text())) {
      sink.push(`console: ${msg.text()}`);
    }
    // Engine font-fallback failures mean a codepoint silently degraded
    // to tofu — they surface as warnings, so watch for the phrases too
    // (the engine's own 404 wording is "Permanent HTTP failure", and
    // exhaustion is "permanently unavailable").
    if (
      (msg.type() === "warning" || msg.type() === "error") &&
      /permanently unavailable|Permanent HTTP failure|Failed to load font/.test(
        msg.text(),
      )
    ) {
      sink.push(`font: ${msg.text()}`);
    }
    // pdfium worker failures: pdfium_client.js logs "Worker error:" when
    // the wasm worker fails to load or parse — the exact signature of a
    // broken worker bootstrap (a garbled or TT-blocked worker script
    // still constructs the Worker object, so the workerCount assertion
    // alone cannot catch it).
    if (
      msg.type() === "error" &&
      /Worker error|Failed to load pdfium/.test(msg.text())
    ) {
      sink.push(`pdfium: ${msg.text()}`);
    }
  });
  page.on("pageerror", (err) => {
    if (pattern.test(String(err))) {
      sink.push(`pageerror: ${err}`);
    }
  });
}

// Records every request whose origin differs from the page origin (#3228).
// Attach before the first navigation. blob:/data: URLs are in-process
// artifacts, and same-host upgrades (ws:) are the workspace WebSocket.
// The origin comes from API_BASE, not page.url() — at attach time the page
// is still about:blank. This is belt-and-braces on top of the console
// watch: a CSP-blocked external fetch is reliably reported as a console
// violation, while this catches anything that would load silently.
function watchExternalRequests(page: Page, sink: string[]) {
  const selfOrigin = new URL(API_BASE).origin;
  page.on("request", (req) => {
    const url = req.url();
    if (url.startsWith("blob:") || url.startsWith("data:")) return;
    let origin: string;
    try {
      origin = new URL(url).origin;
    } catch {
      return;
    }
    if (origin !== selfOrigin) {
      sink.push(url);
    }
  });
}

// Keep every resource-timing entry in each document the page loads: the
// default 250-entry buffer fills in a full session, and once full, NEW
// entries are dropped — including the fallback-font fetches the #3228
// assertions read. Runs before app code via addInitScript (so it also
// re-applies in the fresh document after the pdf deep-link reload).
async function initResourceBuffer(page: Page) {
  await page.addInitScript(() => {
    performance.setResourceTimingBufferSize(10000);
  });
}

// True when a vendored fallback-font part was served from this origin —
// proves the engine's fallback chain resolved locally. There is no
// deterministic boot-time trigger: the flutter tool bundles a Roboto
// family in FontManifest (uses-material-design), so the engine skips its
// boot-time Roboto download entirely — the ONLY trigger is missing-glyph
// text actually rendering (the CJK terminal step below). Keep this
// assertion downstream of that step; hoisting it above the terminal phase
// turns it into a guaranteed timeout.
async function fetchedFallbackFont(page: Page): Promise<boolean> {
  return page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .some((e) => e.name.includes("/assets/assets/fallback-fonts/")),
  );
}

// A minimal single-page PDF (same fixture as the pdf file-viewer spec).
const PDF = Buffer.from(
  "%PDF-1.1\n" +
    "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" +
    "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n" +
    "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>endobj\n" +
    "4 0 obj<</Length 44>>stream\n1 0 0 1 20 80 cm BT (hi) Tj ET\nendstream endobj\n" +
    "trailer<</Root 1 0 R>>\n%%EOF\n",
  "latin1",
);

test.describe("CSP / Trusted Types (#3219)", () => {
  test("login → workspace → terminal → files → pdf → download, zero violations, zero external fetches", async ({
    page,
    request,
  }) => {
    const violations: string[] = [];
    watchViolations(page, violations);
    watchExternalRequests(page, violations);
    await initResourceBuffer(page);
    if (process.env.KL_CSP_DEBUG) {
      page.on("console", (m) =>
        console.log(`[console.${m.type()}]`, m.text().slice(0, 300)),
      );
      page.on("pageerror", (e) =>
        console.log("[pageerror]", String(e).slice(0, 300)),
      );
    }

    // Positive pdfium signals: count Worker constructions (the wasm
    // worker is a TrustedScriptURL sink that TT enforcement rejects on a
    // regression), so the spec proves the viewer's worker came up, not
    // merely that nothing was logged. Worker construction alone does
    // NOT prove the worker script parses — that failure is caught by
    // the "Worker error" console watch above.
    await page.addInitScript(() => {
      (window as any).__workerCount = 0;
      const OrigWorker = window.Worker;
      class CountingWorker extends OrigWorker {
        constructor(scriptUrl: string | URL, options?: WorkerOptions) {
          super(scriptUrl, options);
          (window as any).__workerCount += 1;
        }
      }
      Object.defineProperty(window, "Worker", {
        configurable: true,
        writable: true,
        value: CountingWorker,
      });
    });

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "csp-tt",
      { waitForTerminal: true },
    );
    try {
      // Terminal: run a command through the flterm canvas. The cat'd CJK
      // file makes the engine's font fallback chain fire for codepoints
      // the bundled fonts lack (#3228): with the vendored set they resolve
      // same-origin, and a regression back to fonts.gstatic.com is a hard
      // CSP-violation failure above. The seeded-file `cat` is the real
      // guarantee (keyboard delivery of non-ASCII is unreliable); the
      // echoed literal is just extra pressure in the same render.
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/cjk.txt",
        "中文测试 … ✓\n",
        headers,
        "text/plain",
      );
      await terminalType(page, "echo '中文测试 … ✓' && cat ~/cjk.txt");
      await page.waitForTimeout(1500);

      // #3228 (checked before the pdf deep-link reload below — a reload
      // starts a fresh document whose resource buffer loses these): the
      // fallback chain resolved from the vendored same-origin parts.
      await expect
        .poll(() => fetchedFallbackFont(page), { timeout: 30_000 })
        .toBe(true);

      // File upload (API) + text-file view in the UI.
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/notes.txt",
        "hello csp\n",
        headers,
        "text/plain",
      );
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/doc.pdf",
        PDF,
        headers,
        "application/pdf",
      );
      await openFilesTab(page);
      await page.waitForTimeout(500);
      await clickFileRow(page, 0); // notes.txt viewer
      await page.waitForTimeout(1000);

      // PDF viewer (deep-link into the existing viewer): pdfrx loads a
      // script tag + wasm + a blob worker — all TT/CSP-sensitive paths.
      // The ?file= param is the absolute in-workspace path (a bare name
      // 400s at files/download and the viewer never mounts).
      await page.goto(
        `/#/workspace/${workspaceId}?file=${encodeURIComponent("/home/klangk/doc.pdf")}`,
        { waitUntil: "load" },
      );

      // The two TT sinks the review found: the pdfium_client.js asset
      // must have loaded (plain-string script.src) and the wasm Worker
      // must have been constructed — both fail under a Trusted Types
      // regression. Poll instead of sleeping a fixed budget: the
      // unminified bundle's boot + viewer mount routinely exceeds any
      // fixed wait on a loaded runner.
      await expect
        .poll(() => page.evaluate(() => (window as any).__workerCount ?? 0), {
          timeout: 60_000,
        })
        .toBeGreaterThan(0);
      await expect
        .poll(
          () =>
            page.evaluate(() =>
              performance
                .getEntriesByType("resource")
                .some((e) => e.name.includes("pdfium_client.js")),
            ),
          { timeout: 30_000 },
        )
        .toBe(true);

      // Download round-trip.
      const dl = await request.get(
        `${API_BASE}/api/v1/workspaces/${workspaceId}/files/download?path=${encodeURIComponent("/home/klangk/notes.txt")}`,
        { headers },
      );
      expect(dl.ok()).toBeTruthy();

      await page.waitForTimeout(1500); // let the console settle
      expect(violations.join("\n---\n") || "(none)").toBe("(none)");
    } finally {
      await cleanup();
    }
  }, 300_000);

  // #3228 acceptance: an offline (externally-blocked) session renders the
  // UI and terminal without tofu beyond what the bundled fonts cover. Every
  // non-same-origin request is aborted at the network layer — stricter
  // than the CSP, which only reports violations — and the run must stay
  // free of both external attempts and font-failure warnings while the
  // vendored fallback parts still load.
  test("offline: externally-blocked session renders UI + CJK terminal from bundled assets", async ({
    page,
    request,
  }) => {
    const violations: string[] = [];
    watchViolations(page, violations);
    await initResourceBuffer(page);
    const externalAttempts: string[] = [];
    const selfOrigin = new URL(API_BASE).origin;
    await page.route("**/*", (route) => {
      const url = route.request().url();
      if (
        url.startsWith("blob:") ||
        url.startsWith("data:") ||
        new URL(url).origin === selfOrigin
      ) {
        return route.continue();
      }
      externalAttempts.push(url);
      return route.abort();
    });

    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "csp-off",
      { waitForTerminal: true },
    );
    try {
      await seedFile(
        request,
        workspaceId,
        "/home/klangk/cjk.txt",
        "中文测试 … ✓\n",
        headers,
        "text/plain",
      );
      await terminalType(page, "echo '中文测试 … ✓' && cat ~/cjk.txt");
      await expect
        .poll(() => fetchedFallbackFont(page), { timeout: 30_000 })
        .toBe(true);
      expect(
        externalAttempts.length ? externalAttempts.join("\n") : "(none)",
        "external requests under a fully self-contained frontend",
      ).toBe("(none)");
      expect(violations.join("\n---\n") || "(none)").toBe("(none)");
    } finally {
      await cleanup();
    }
  }, 300_000);
});
