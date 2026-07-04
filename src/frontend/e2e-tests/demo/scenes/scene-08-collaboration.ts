/**
 * Scene 7 — Multi-User Collaboration (~1.5–2 min)
 *
 * CONTINUITY: the hero (admin@example.com) shares HER OWN `demo` workspace
 * (the one accumulating state since Sc 2/4/5) with the SEEDED supporting cast
 * (teammate/designer/reviewer @example.com — created by demo-seed.ts), not
 * throwaway demo-collab-* users. So the Users panel and presence bar look
 * lived-in, and the chat history from Sc 5 is still here.
 *
 * Four participants + the agent in ONE workspace, to show that talking to the
 * agent can itself be a collaborative process (not just solo Q&A):
 *
 *   - owner     — hero view, the test's `page` (recorded)
 *   - teammate  — second recorded browser context (for side-by-side cuts)
 *   - designer  — WS-only participant (appears in owner's presence + chat)
 *   - reviewer  — WS-only participant (appears in owner's presence + chat)
 *   - clanker   — the AI agent, @-mentioned mid-conversation
 *
 * All three cast humans are added as Collaborators (can type in shared
 * terminals + chat). The owner's single recording carries the whole story: the
 * presence bar shows all four people, and the Chat tab shows interleaved
 * authors discussing, then the owner @mentions clanker, clanker responds, and
 * the others react.
 *
 * The beat order:
 *   1. Owner shares a terminal; teammate joins — real pair-programming
 *      (owner types, appears in both windows).
 *   2. Move to Chat: designer + reviewer chime in via chat (visible in owner's
 *      view as interleaved authors).
 *   3. Owner TYPES the @clanker prompt on-screen (the key visual moment).
 *   4. Hold for clanker's live, nondeterministic reply (trim dead air in DaVinci
 *      or narrate over it).
 *   5. Teammate reacts in chat.
 *
 * Output: TWO videos (owner + teammate) for composite/intercut. designer +
 * reviewer don't need their own recordings — they're seen via owner's presence
 * bar and their chat messages.
 *
 * Reliability note (from docs-screenshots.spec.ts): the Flutter right-click →
 * "Share" popup is flaky to click by coordinate, so we share via the WS command
 * (`share_window`) — proven reliable in the existing screenshot suite. The
 * visual result (broadcast icon, teammate's shared tab, viewer count) is
 * identical. Add a VO beat over it.
 */
import { test } from "@playwright/test";
import path from "path";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  DEMO_PASSWORD,
  SHARED_WORKSPACE,
  DEMO_TEAMMATE_EMAIL,
  DEMO_DESIGNER_EMAIL,
  DEMO_REVIEWER_EMAIL,
  pace,
  slowType,
  vp,
  mouseClick,
  terminalType,
  terminalTabCenterPx,
  openChatTab,
  apiLogin,
  addRole,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  connectWs,
  DemoWs,
} from "../demo-helpers";

const CLANKER_PROMPT =
  "@clanker scaffold a simple Flask landing page with a headline and a button";

// Where the teammate's video is written (owner's goes to Playwright's default
// output dir alongside the test).
const VIDEO_DIR = path.resolve(__dirname, "..", "test-results", "demo-videos");

test("collaboration", async ({ page, browser, request }) => {
  test.setTimeout(300_000);

  // --- 1. Shared `demo` workspace; add the three cast collaborators. They
  //         already exist (demo-seed.ts), so ensureUser-style apiLogin works
  //         directly. The owner is the hero. No workspace wipe (continuity). ---
  const { headers: ownerHeaders } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(
    request,
    ownerHeaders,
    SHARED_WORKSPACE,
  );
  await addRole(
    request,
    ownerHeaders,
    ws.id,
    "collaborators",
    DEMO_TEAMMATE_EMAIL,
  );
  await addRole(
    request,
    ownerHeaders,
    ws.id,
    "collaborators",
    DEMO_DESIGNER_EMAIL,
  );
  await addRole(
    request,
    ownerHeaders,
    ws.id,
    "collaborators",
    DEMO_REVIEWER_EMAIL,
  );

  // --- 2. Owner opens the workspace (hero recording) ---
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD);
  await pace(1500);

  // --- 3. Share the owner's terminal via WS (reliable; see header note).
  //      Share the SCRATCH tab (a plain bash shell) by NAME, NOT windows[0]
  //      (which is the bash tab where pi lives from Sc 5b). Typing the
  //      pair-programming beat into pi's tab would pollute pi's conversation
  //      context ahead of Sc 8 (which reuses the running pi). Scratch is a
  //      plain shell, so "owner typing here" lands harmlessly. ---
  const ownerWs = await connectWs(
    (await apiLogin(request, DEMO_HERO_EMAIL, DEMO_HERO_PASSWORD)).token,
    ws.id,
  );
  ownerWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
  const windows = await ownerWs.recvUntil((m) => m.type === "terminal_windows");
  const allWindows = windows.windows as Array<Record<string, unknown>>;
  // Prefer "scratch" (plain shell); fall back to the last window (never the
  // first, which is pi's bash tab).
  const shareWin =
    allWindows.find((w) => w.name === "scratch") ??
    allWindows[allWindows.length - 1];
  ownerWs.send({ cmd: "share_window", window_id: shareWin.id as string });
  await ownerWs.recvUntil((m) => m.type === "shared_terminals");
  await pace(1500); // broadcast icon appears on owner's tab

  // --- 4. Teammate opens in its own RECORDING context ---
  const teammateContext = await browser.newContext({
    recordVideo: { dir: VIDEO_DIR },
    viewport: { width: 960, height: 540 },
  });
  const teammatePage = await teammateContext.newPage();
  // The owner already booted this workspace's container, so it won't
  // re-emit `container_ready` — open without waiting for it (the
  // container is up; the teammate just attaches).
  await openWorkspaceDemo(
    teammatePage,
    DEMO_TEAMMATE_EMAIL,
    ws.id,
    DEMO_PASSWORD,
    { waitForContainer: false },
  );
  await pace(1500);

  // --- 5. designer + reviewer join via WS (presence, no browser pages) ---
  const designerWs = await connectWs(
    (await apiLogin(request, DEMO_DESIGNER_EMAIL, DEMO_PASSWORD)).token,
    ws.id,
  );
  const reviewerWs = await connectWs(
    (await apiLogin(request, DEMO_REVIEWER_EMAIL, DEMO_PASSWORD)).token,
    ws.id,
  );
  await pace(1500); // presence bar now shows all four people

  // --- 6. Teammate joins the shared terminal; pair-program beat ---
  const teammateWs = await connectWs(
    (await apiLogin(request, DEMO_TEAMMATE_EMAIL, DEMO_PASSWORD)).token,
    ws.id,
  );
  teammateWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
  await teammateWs.recvUntil((m) => m.type === "terminal_started");
  const shared = await teammateWs.recvUntil(
    (m) =>
      m.type === "shared_terminals" &&
      (m.terminals as Array<Record<string, unknown>>).length > 0,
  );
  const sharedTerm = (shared.terminals as Array<Record<string, unknown>>)[0];
  teammateWs.send({
    cmd: "join_shared_terminal",
    user_id: sharedTerm.user_id as string,
    window_id: sharedTerm.window_id as string,
  });
  await teammateWs.recvUntil((m) => m.type === "terminal_started");
  await pace(2000);

  // Owner TYPES in the shared SCRATCH tab (visually switch to it first so the
  // on-camera keystrokes land in the plain shell, not pi's bash tab where pi
  // is still running from Sc 5b — typing into pi would pollute its context
  // ahead of Sc 8). The teammate sees the same shared pane.
  await mouseClick(page, terminalTabCenterPx(2), vp(page).height * 0.2);
  await pace(1000);
  await terminalType(page, "echo 'owner typing here'"); // owner's window
  await pace(2500);
  await terminalType(teammatePage, "echo 'teammate typing back'"); // teammate's window
  await pace(2500);

  // --- 7. Collaborative chat beat: humans discuss, then @mention clanker ---
  await openChatTab(page); // owner's view is the hero for the chat exchange
  await pace(1500);

  // designer kicks it off, reviewer chimes in (both via WS — their messages
  // land in owner's chat view as interleaved authors).
  await sendChat(designerWs, "hey, can we add a simple landing page?");
  await pace(2500);
  await sendChat(reviewerWs, "yeah — minimal, just a headline and a button");
  await pace(2500);

  // Owner TYPES the @clanker prompt on-screen (the key visual moment). Trailing
  // space closes the @-autocomplete so Enter sends (see chat_input_bar.dart).
  const { width, height } = vp(page);
  await mouseClick(page, width / 2, height - 25); // focus chat input bar
  await pace(500);
  await slowType(page, `${CLANKER_PROMPT} `, { cps: 14 });
  await pace(600);
  await page.keyboard.press("Enter");
  await pace(2000);

  // --- 8. Hold for clanker's live reply (nondeterministic). Trim the dead air
  //         or narrate over it in DaVinci. Tune with KLANGK_DEMO_AGENT_WAIT. ---
  const waitMs = Number(process.env.KLANGK_DEMO_AGENT_WAIT || 60_000);
  await pace(waitMs);

  // --- 9. Teammate reacts (closes the collaborative loop) ---
  await sendChat(teammateWs, "nice — let's wire that button up next");
  await pace(3000);

  // --- Cleanup ---
  [designerWs, reviewerWs, teammateWs, ownerWs].forEach((c) => c.close());
  await teammatePage.close();
  await teammateContext.close();
});

/** Send a chat message over WS and let it broadcast, with a small settle. */
async function sendChat(ws: DemoWs, message: string) {
  ws.send({ cmd: "chat_send", message });
}
