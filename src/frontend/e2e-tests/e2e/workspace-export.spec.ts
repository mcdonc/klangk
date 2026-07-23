import { test, expect } from "@playwright/test";
import { execSync } from "child_process";
import { mkdtempSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import {
  API_BASE,
  loginViaUI,
  waitForFlutter,
  connectContainer,
  createWorkspace,
  seedFile,
  vp,
  flutterClick,
} from "./helpers";

// The File System Access API's showSaveFilePicker() opens a native file dialog
// that Playwright cannot drive (microsoft/playwright#8850 — no filechooser /
// download event is raised for it, and the W3C web-platform-tests for it are
// themselves manual). So we inject a fake showSaveFilePicker() that records the
// chunks the real streaming code writes — exercising the actual production path
// (authenticated fetch → ReadableStream reader loop → chunked write) against an
// in-memory sink instead of disk. See workspace_settings_panel._exportWorkspace
// → downloadStreamedUrl (#700).
const INSTALL_FSA_SHIM = `
(() => {
  window.__exportChunks = [];
  window.__exportDone = false;
  window.__exportPickerCalled = false;
  window.__exportSuggestedName = null;
  window.__exportError = null;
  window.showSaveFilePicker = function (options) {
    window.__exportPickerCalled = true;
    window.__exportSuggestedName = (options && options.suggestedName) || null;
    return Promise.resolve({
      createWritable: function () {
        return Promise.resolve({
          write: function (chunk) {
            try {
              // chunk is a Uint8Array from the real ReadableStream reader.
              // Copy it — the underlying buffer is reused across reads.
              window.__exportChunks.push(new Uint8Array(chunk));
            } catch (e) {
              window.__exportError = String(e);
            }
            return Promise.resolve();
          },
          close: function () {
            window.__exportDone = true;
            return Promise.resolve();
          },
        });
      },
    });
  };
})();
`;

// Admin account provisioned by global-setup (KLANGKD_DEFAULT_USER / PASSWORD).
// The export endpoint is admin-only (workspaces.py: has_permission("admin")).
const ADMIN_EMAIL = "admin@example.com";
const ADMIN_PASSWORD = "admin";

/** Poll the shim's window globals until the stream finishes (or errors).
 *  Returns only cheap status fields — the bytes themselves are retrieved
 *  separately by retrieveStreamedBytes() so a large archive isn't squeezed
 *  through a single page.evaluate return (which CDP truncates). */
async function pollStreamedExport(
  page: import("@playwright/test").Page,
  timeout = 60_000,
): Promise<{
  pickerCalled: boolean;
  done: boolean;
  error: string | null;
  suggestedName: string | null;
  length: number;
}> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const r = await readShimState(page);
    if (r.done || r.error) return r;
    await page.waitForTimeout(250);
  }
  const final = await readShimState(page);
  throw new Error(
    `Streamed export did not complete within ${timeout}ms. Final shim state: ${JSON.stringify(
      {
        pickerCalled: final.pickerCalled,
        done: final.done,
        error: final.error,
        suggestedName: final.suggestedName,
        length: final.length,
      },
    )}`,
  );
}

/** Read the shim's cheap status fields. */
async function readShimState(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const w = window as any;
    const chunks: Uint8Array[] = w.__exportChunks || [];
    let total = 0;
    for (const c of chunks) total += c.length;
    return {
      pickerCalled: w.__exportPickerCalled === true,
      done: w.__exportDone === true,
      error: w.__exportError || null,
      suggestedName: w.__exportSuggestedName || null,
      length: total,
    };
  });
}

/** Retrieve the full streamed byte buffer from the shim, in fixed-size slices.
 *  Returning the whole archive through one page.evaluate call gets truncated
 *  by CDP, so fetch it in 4 MB raw slices (~5.3 MB base64 each). */
async function retrieveStreamedBytes(
  page: import("@playwright/test").Page,
): Promise<Buffer> {
  // Materialise the merged buffer once on the page side.
  await page.evaluate(() => {
    const chunks: Uint8Array[] = (window as any).__exportChunks || [];
    let total = 0;
    for (const c of chunks) total += c.length;
    const merged = new Uint8Array(total);
    let o = 0;
    for (const c of chunks) {
      merged.set(c, o);
      o += c.length;
    }
    (window as any).__exportMerged = merged;
  });
  const total: number = await page.evaluate(
    () => (window as any).__exportMerged.length,
  );
  const SLICE = 4 * 1024 * 1024;
  const parts: Buffer[] = [];
  for (let off = 0; off < total; off += SLICE) {
    const len = Math.min(SLICE, total - off);
    const b64 = await page.evaluate(
      ([start, n]) => {
        const m: Uint8Array = (window as any).__exportMerged;
        const slice = m.subarray(start, start + n);
        let s = "";
        for (let i = 0; i < slice.length; i++)
          s += String.fromCharCode(slice[i]);
        return btoa(s);
      },
      [off, len] as [number, number],
    );
    parts.push(Buffer.from(b64, "base64"));
  }
  return Buffer.concat(parts);
}

test.describe("Workspace export streaming (#700)", () => {
  test("streams the export through the File System Access API on chromium", async ({
    page,
    request,
  }) => {
    test.setTimeout(180_000);

    // --- 1. Admin setup via API: workspace + running container + marker file.
    // Track the export network request/response so a failed/missing fetch is
    // visible in the failure message rather than a silent 60s timeout.
    const exportReqs: { method: string; status: number; url: string }[] = [];
    page.on("response", (resp) => {
      if (resp.url().includes("/export")) {
        exportReqs.push({
          method: resp.request().method(),
          status: resp.status(),
          url: resp.url(),
        });
      }
    });
    const loginResp = await request.post(`${API_BASE}/api/v1/auth/login`, {
      data: { identifier: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    expect(loginResp.ok()).toBeTruthy();
    const adminToken = (await loginResp.json()).access_token;
    const adminHeaders = { Authorization: `Bearer ${adminToken}` };

    const { workspaceId, cleanup } = await createWorkspace(
      request,
      adminHeaders,
      "e2e-export-stream",
    );
    await connectContainer(workspaceId, adminToken);

    const marker = `streaming-export-marker-${Date.now()}\n`;
    const markerPath = "/home/work/export_marker.txt";
    await seedFile(request, workspaceId, markerPath, marker, adminHeaders);

    try {
      // --- 2. Install the FSA shim before the app loads.
      await page.addInitScript(INSTALL_FSA_SHIM);

      // --- 3. Log in as admin and open the workspace, waiting for the UI to
      //         finish connecting (container_ready) so the page is stable
      //         before we touch accessibility semantics. Mirrors openWorkspace.
      const uiReady = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error("UI did not report container_ready")),
          120_000,
        );
        page.on(
          "websocket",
          (ws: { on: (e: string, cb: Function) => void }) => {
            ws.on("framereceived", (frame: { payload: string | Buffer }) => {
              if (frame.payload.toString().includes("container_ready")) {
                clearTimeout(timer);
                resolve();
              }
            });
          },
        );
      });
      await loginViaUI(page, ADMIN_EMAIL, ADMIN_PASSWORD);
      await page.goto(`/#/workspace/${workspaceId}`, { waitUntil: "load" });
      await waitForFlutter(page);
      await uiReady;
      await page.waitForTimeout(1000); // let the terminal render

      // --- 4. Enable Flutter Web accessibility semantics. The app renders to a
      //         <canvas>; until a11y is on, real widgets (tabs, the Export
      //         button) are invisible to role locators. Flutter injects an
      //         "Enable accessibility" invoker a few seconds after load (often in
      //         shadow DOM, parked off-screen), so wait for it to attach, then
      //         click it programmatically via the resolved element — that bypasses
      //         both the off-screen position and the actionability checks a normal
      //         or force click enforces. Enabling populates <flt-semantics>, which
      //         we wait for before proceeding.
      const a11yInvoker = page.getByRole("button", {
        name: /^Enable accessibility$/i,
      });
      await a11yInvoker.waitFor({ state: "attached", timeout: 30_000 });
      await a11yInvoker.evaluate((el) => (el as HTMLElement).click());
      await expect
        .poll(
          () =>
            page.evaluate(
              () => document.querySelectorAll("flt-semantics *").length,
            ),
          { timeout: 15_000, message: "flt-semantics tree populated" },
        )
        .toBeGreaterThan(0);

      // --- 5. Open the Settings tab (5th of 5 owner tabs, at y~76).
      const { width } = vp(page);
      const tabWidth = width / 5;
      await flutterClick(page, tabWidth * 4 + tabWidth / 2, 76);
      await page.waitForTimeout(800);

      // --- 6. Click "Export Workspace" via its accessible role (it's left-
      //         aligned in a centered 500px column, so a coordinate click is
      //         unreliable). Its name includes the download icon + label.
      const exportBtn = page.getByRole("button", { name: /Export Workspace/i });
      await exportBtn.click({ timeout: 15_000 });

      // --- 7. Wait for the stream to finish and read back what the shim captured.
      let result;
      try {
        result = await pollStreamedExport(page);
      } catch (e) {
        // Surface the export network activity so a missing/failed fetch is
        // diagnosable instead of a bare 60s timeout.
        // eslint-disable-next-line no-console
        console.log("EXPORT_REQS_AT_FAILURE:", JSON.stringify(exportReqs));
        throw e;
      }

      // The streaming path was actually taken (not the buffered fallback).
      expect(
        result.pickerCalled,
        "showSaveFilePicker was invoked",
      ).toBeTruthy();
      expect(result.error, "no error during writable.write()").toBeNull();
      expect(result.done, "writable.close() was called").toBeTruthy();
      expect(result.suggestedName, "suggested name ends in .tar.gz").toMatch(
        /\.tar\.gz$/,
      );

      // --- 7. The captured bytes are a valid gzip archive.
      const streamed = await retrieveStreamedBytes(page);
      expect(streamed.length, "archive is non-empty").toBeGreaterThan(0);
      expect(streamed[0]).toBe(0x1f); // gzip magic
      expect(streamed[1]).toBe(0x8b);

      // --- 8. Strongest check: the archive gunzips, lists, and the seeded
      //         marker file round-trips byte-for-byte (proves the streamed
      //         download is complete and correct, not just plausible bytes).
      const tmpDir = mkdtempSync(join(tmpdir(), "klangk-export-stream-"));
      const archivePath = join(tmpDir, "export.tar.gz");
      writeFileSync(archivePath, streamed);

      let entries: string;
      try {
        entries = execSync(`tar tzf "${archivePath}"`, { encoding: "utf-8" });
      } catch (e) {
        // Diagnose a malformed archive: is the capture truncated vs. the
        // ground-truth API export, reordered, or just not gzip?
        const apiExport = await request.get(
          `${API_BASE}/api/v1/workspaces/${workspaceId}/export`,
          { headers: adminHeaders },
        );
        const apiBody = apiExport.ok()
          ? await apiExport.body()
          : Buffer.alloc(0);
        const gunzipTest = (() => {
          try {
            execSync(`gunzip -t "${archivePath}"`, {
              encoding: "utf-8",
              stdio: "pipe",
            });
            return "ok";
          } catch (ge: any) {
            return (ge.stderr || ge.message || "").toString().trim();
          }
        })();
        // eslint-disable-next-line no-console
        console.log(
          "TAR_FAIL_DIAG:",
          JSON.stringify({
            streamedLength: streamed.length,
            apiLength: apiBody.length,
            prefixMatch:
              apiBody.length >= streamed.length &&
              apiBody.subarray(0, streamed.length).equals(streamed),
            gunzipTest,
          }),
        );
        throw e;
      }
      const markerEntry = entries
        .split("\n")
        .find((l) => l.endsWith("home/work/export_marker.txt"));
      expect(
        markerEntry,
        "seeded marker file is present in the archive",
      ).toBeTruthy();

      const extracted = execSync(
        `tar -xOzf "${archivePath}" "${markerEntry}"`,
        {
          encoding: "utf-8",
        },
      );
      expect(extracted).toBe(marker);
    } finally {
      await cleanup();
    }
  });
});
