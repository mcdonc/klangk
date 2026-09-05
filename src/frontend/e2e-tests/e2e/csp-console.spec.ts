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
// Known-exception filter: the Flutter engine's CanvasKit renderer lazily
// fetches per-script Noto fallback fonts (NotoSansSC/TC/HK/JP, Symbols2)
// from fonts.gstatic.com when a frame rasterizes a codepoint the bundled
// fonts lack (e.g. the "…" in "Signing in…", or CJK bytes in terminal
// output). Those fetches are blocked by the pre-existing first-party
// connect-src 'self' (#3149) and are not a script-execution or TT sink —
// they predate this change and are flaky (only fire when such a frame
// renders). Everything else — script-src, require-trusted-types-for, and
// any other directive — must stay violation-free.
function isKnownFontFallbackNoise(text: string): boolean {
  // Shape-agnostic across browsers: Chromium says "Connecting to '<url>'
  // ... connect-src" plus a directive-less companion "Fetch API cannot
  // load <url>. Refused to connect"; Firefox says "blocked the loading
  // of a resource (connect-src) at <url>". Require BOTH the fallback-font
  // host AND fetch-flavored wording — "Refused to connect" is exclusive
  // to fetches (script-src violations say "Refused to execute"; TT ones
  // "requires 'Trusted...'"), so a script or TT violation naming that
  // host still fails the suite.
  if (!/fonts\.gstatic\.com\/s\//.test(text)) return false;
  return /connect-src|font-src|Refused to connect/.test(text);
}

function watchViolations(page: Page, sink: string[]) {
  // "Trusted" covers Trusted Types, TrustedScriptURL, and TrustedHTML —
  // Chromium's TT sink message is "This document requires
  // 'TrustedScriptURL' assignment. The action has been blocked.", which
  // none of the older alternatives match.
  const pattern =
    /Content Security Policy|Refused to|Trusted|trustedTypes|violates|CSP/;
  page.on("console", (msg) => {
    if (msg.type() === "error" && pattern.test(msg.text())) {
      if (!isKnownFontFallbackNoise(msg.text())) {
        sink.push(`console: ${msg.text()}`);
      }
    }
  });
  page.on("pageerror", (err) => {
    if (pattern.test(String(err))) {
      sink.push(`pageerror: ${err}`);
    }
  });
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
  test("login → workspace → terminal → files → pdf → download, zero violations", async ({
    page,
    request,
  }) => {
    const violations: string[] = [];
    watchViolations(page, violations);

    // Positive pdfium signals: count Worker constructions (pdfrx builds
    // its wasm worker from a blob: URL — a TrustedScriptURL sink that TT
    // enforcement rejects on a regression), so the spec proves the viewer
    // came up, not merely that nothing was logged.
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
      // Terminal: run a command through the flterm canvas.
      await terminalType(page, "echo CSP_OK");
      await page.waitForTimeout(1500);

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
      // NOTE: the ?file= param is an ABSOLUTE container path (openFile uses
      // it verbatim) — a bare name 400s at files/download and the viewer
      // never mounts pdfrx.
      await page.goto(
        `/#/workspace/${workspaceId}?file=${encodeURIComponent("/home/klangk/doc.pdf")}`,
        { waitUntil: "load" },
      );
      // Fresh page boot (canvaskit + WS + container_ready) under CI load can
      // exceed a fixed wait — poll for the viewer to actually come up.
      await expect
        .poll(() => page.evaluate(() => (window as any).__workerCount ?? 0), {
          timeout: 30_000,
        })
        .toBeGreaterThan(0);

      // The two TT sinks the review found: the pdfium_client.js asset
      // must have loaded (plain-string script.src) and the wasm Worker
      // must have been constructed (blob: URL) — both fail under a
      // Trusted Types regression.
      const workerCount = await page.evaluate(
        () => (window as any).__workerCount ?? 0,
      );
      expect(workerCount, "pdfium wasm Worker constructed").toBeGreaterThan(0);
      const pdfiumLoaded = await page.evaluate(() =>
        performance
          .getEntriesByType("resource")
          .some((e) => e.name.includes("pdfium_client.js")),
      );
      expect(pdfiumLoaded, "pdfium_client.js loaded (TT script.src sink)").toBe(
        true,
      );

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
});
