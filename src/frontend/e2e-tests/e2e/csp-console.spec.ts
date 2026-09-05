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
const FONT_FALLBACK_FETCH =
  /^((Fetch API cannot load )?https:\/\/fonts\.gstatic\.com\/s\/|.*(Connecting|Loading) to 'https:\/\/fonts\.gstatic\.com\/s\/)/;

function isKnownFontFallbackNoise(text: string): boolean {
  if (!FONT_FALLBACK_FETCH.test(text)) return false;
  return /connect-src|font-src|Refused to connect/.test(text);
}

function watchViolations(page: Page, sink: string[]) {
  const pattern =
    /Content Security Policy|Refused to|Trusted Types|trustedTypes|violates|CSP/;
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
      await page.goto(`/#/workspace/${workspaceId}?file=doc.pdf`, {
        waitUntil: "load",
      });
      await page.waitForTimeout(4000);

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
