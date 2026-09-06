import { expect, test } from "@playwright/test";

import {
  TEST_PASSWORD,
  createWorkspace,
  loginOnCurrentPage,
  registerUser,
  terminalType,
  waitForFlutter,
} from "../e2e/helpers";

// Subpath × web × DPoP (#3287): the whole browser flow behind an outer
// proxy serving the app at /klangk/ with the prefix stripped on forward
// and X-Forwarded-Prefix set — the documented nginx pattern. DPoP
// binding is active (the default browser path), so a proof whose htu
// mishandles the base path fails login's first authenticated call.
//
// Covers every gate the fix touches:
// - the HTTP gate: my-permissions answers 200 (not 401 uri mismatch)
//   after login+bind;
// - the refresh gate: NOT exercised here — refresh fires at 80% of a
//   token lifetime measured in hours, which no e2e flow spans. Its
//   tolerance is unit-tested (test_auth.py:
//   test_bound_refresh_tolerates_prefixed_htu) and the client's refresh
//   minting rides the same authHeadersFor covered below;
// - the main WS gate: the /klangk/ws connect completes and streams
//   terminal output (container_ready → terminal_started → echo);
// - the decider WS gate: the /klangk/ws/consent-decider connect
//   completes and delivers its egress_rules frame.

// The prefix every browser URL carries in this deployment. Playwright's
// goto("/") resolves as an ABSOLUTE path (it drops the baseURL's /klangk
// segment), so this spec always navigates with the prefix spelled out.
const PREFIX = "/klangk";

/** The path of a DPoP proof's htu claim (the JWT payload's URI).
 *
 * The client must mint the htu backend-visible (#3287): the path the
 * server sees (prefix stripped), never `PREFIX + path`. The server
 * tolerates the prefixed form for already-deployed clients, so the
 * behavioral 200s alone cannot catch a minting regression — this
 * decodes the actual proofs and pins their shape. */
function htuPathOf(proof: string): string | null {
  const payload = proof.split(".")[1];
  if (!payload) return null;
  const json = Buffer.from(
    payload.replace(/-/g, "+").replace(/_/g, "/"),
    "base64",
  ).toString("utf8");
  try {
    const htu = JSON.parse(json).htu;
    return typeof htu === "string" ? new URL(htu, "http://x").pathname : null;
  } catch {
    return null;
  }
}

test.describe.serial("subpath deployment × DPoP (#3287)", () => {
  test("login+bind, authenticated API, and both WS gates pass behind /klangk", async ({
    page,
    request,
  }) => {
    // Fail-loud guard: no request in the whole flow may 401 with a
    // DPoP proof complaint.
    const dpopFailures: string[] = [];
    page.on("response", async (resp) => {
      if (resp.status() !== 401) return;
      const body = await resp.text().catch(() => "");
      if (body.includes("Invalid DPoP proof")) {
        dpopFailures.push(`${resp.url()} -> ${body}`);
      }
    });

    // Latch both WebSocket gates before any navigation so no frame can
    // slip past an attached-too-late listener.
    let resolveTerminal: () => void;
    let resolveDecider: () => void;
    const terminalStarted = new Promise<void>((resolve) => {
      resolveTerminal = resolve;
    });
    const deciderRules = new Promise<void>((resolve) => {
      resolveDecider = resolve;
    });
    const wsUrls: string[] = [];
    const terminalOutput: string[] = [];
    const httpProofs = new Map<string, string>();
    page.on("request", (req) => {
      const proof = req.headers()["dpop"];
      if (proof) httpProofs.set(req.url(), proof);
    });
    page.on("websocket", (ws) => {
      wsUrls.push(ws.url());
      ws.on("framereceived", (frame: { payload: string | Buffer }) => {
        const text = frame.payload.toString();
        if (text.includes("terminal_started")) resolveTerminal!();
        if (
          ws.url().includes("/ws/consent-decider") &&
          text.includes("egress_rules")
        ) {
          resolveDecider!();
        }
        if (text.includes('"terminal_output"')) terminalOutput.push(text);
      });
    });

    // Setup over the API (direct to the stack's klangkd): a user who
    // may create workspaces, and one workspace to open.
    const email = `subpath-dpop-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "subpath-dpop",
    );

    try {
      // The document itself must come up under the prefix (assets ride
      // the rewritten <base href="/klangk/">).
      await page.goto(`${PREFIX}/`);
      await waitForFlutter(page);

      // Login + DPoP bind, then the first authenticated call.
      const permissionsResp = page.waitForResponse(
        (r) =>
          r.url().includes("/api/v1/my-permissions") &&
          r.request().method() === "GET",
        { timeout: 60_000 },
      );
      await loginOnCurrentPage(page, email, TEST_PASSWORD);
      await expect(page).toHaveTitle(/Workspaces/i, { timeout: 30_000 });
      expect((await permissionsResp).status()).toBe(200);

      // Open the workspace: container boot, main-WS connect, PTY.
      await page.goto(`${PREFIX}/#/workspace/${workspaceId}`, {
        waitUntil: "load",
      });
      await waitForFlutter(page);
      // Dismiss the "Enable accessibility" overlay if it came up (the
      // in-suite helper is private; same shape as helpers.ts).
      const a11y = page.locator("button", {
        hasText: "Enable accessibility",
      });
      if (await a11y.isVisible({ timeout: 500 }).catch(() => false)) {
        await a11y.click();
        await page.waitForTimeout(300);
      }
      await terminalStarted;

      // The main socket connected through the prefixed path.
      const mainWsUrl = wsUrls.find((u) => /^ws:\/\/[^/]+\/klangk\/ws/.test(u));
      expect(
        mainWsUrl,
        `no /klangk/ws connect among ${JSON.stringify(wsUrls)}`,
      ).toBeTruthy();

      // The proofs name the backend-visible paths, not the prefixed
      // ones (#3287): decode the my-permissions proof header and the
      // main socket's proof query parameter and pin their htu.
      const permissionsProof = [...httpProofs.entries()].find(([url]) =>
        url.includes("/klangk/api/v1/my-permissions"),
      );
      expect(
        permissionsProof,
        "no DPoP header seen on my-permissions",
      ).toBeTruthy();
      expect(htuPathOf(permissionsProof![1])).toBe("/api/v1/my-permissions");
      const wsProof = mainWsUrl
        ? new URL(mainWsUrl).searchParams.get("dpop")
        : null;
      expect(
        wsProof,
        "no DPoP proof parameter on the /klangk/ws connect",
      ).toBeTruthy();
      expect(htuPathOf(wsProof!)).toBe("/ws");

      // Terminal output is observable: type a marker and read it back
      // from the PTY stream.
      await terminalType(page, "echo subpath-dpop-ok");
      await expect
        .poll(() => terminalOutput.join("").includes("subpath-dpop-ok"), {
          timeout: 30_000,
        })
        .toBe(true);

      // The decider socket connected through the prefixed path too and
      // passed its own DPoP gate (rules arrive only after it opens).
      expect(
        wsUrls.some((u) => u.includes("/klangk/ws/consent-decider")),
        `no /klangk/ws/consent-decider connect among ${JSON.stringify(wsUrls)}`,
      ).toBe(true);
      await deciderRules;

      expect(dpopFailures).toEqual([]);
    } finally {
      await cleanup();
    }
  });
});
