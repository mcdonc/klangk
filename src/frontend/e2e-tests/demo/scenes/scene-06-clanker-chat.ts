/**
 * Scene 5 — AI Agent — clanker (~1.5 min)
 *
 * CONTINUITY: still in the hero's `demo` workspace (from Sc 2/4). This is a
 * pure Q&A beat — clanker answers a question in chat; it creates NO files. The
 * Flask app that Sc 5b/6 depend on is built by pi in Sc 5b, not here, so we
 * leave `demo` untouched.
 *
 * Opens the Chat tab, types an @clanker prompt ON SCREEN (so the viewer sees
 * the mention being composed), and lets the live agent respond. This is a
 * nondeterministic / live take — re-run until you like what clanker produced,
 * then keep that recording and trim in DaVinci.
 *
 * Autocomplete gotcha (from chat_input_bar.dart): while the @-mention dropdown
 * is open, Enter ACCEPTS the mention instead of sending. We sidestep it by
 * typing a trailing space after the handle — a space in the mention query
 * auto-closes the dropdown — so the final Enter reliably sends.
 */
import { test } from "@playwright/test";
import {
  DEMO_HERO_EMAIL,
  DEMO_HERO_PASSWORD,
  SHARED_WORKSPACE,
  pace,
  slowType,
  vp,
  mouseClick,
  apiLogin,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  openChatTab,
} from "../demo-helpers";

const PROMPT = '@clanker "how long has this container been up"';

test("clanker chat", async ({ page, request }) => {
  test.setTimeout(300_000);

  // 1. Ensure the shared `demo` workspace exists (continuity). No wipe —
  //    this is a read-only Q&A that leaves the workspace untouched (the
  //    Flask app for Sc 5b/6 is built by pi in Sc 5b).
  const { headers } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(request, headers, SHARED_WORKSPACE);

  // 2. Open it and wait for the container.
  await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD);
  await pace(1200); // let the terminal settle on camera

  // 3. Chat tab (index 2 of 5).
  await openChatTab(page);
  await pace(1000);

  // 4. Focus the input bar (bottom of screen, ~y=695 at 720p) and compose the
  //    prompt slowly so the viewer reads it as it's typed. The trailing space
  //    after "@clanker" closes the mention autocomplete so Enter sends.
  const { width, height } = vp(page);
  await mouseClick(page, width / 2, height - 25);
  await pace(500);
  await slowType(page, PROMPT, { cps: 14 });
  await pace(600);

  // 5. Send.
  await page.keyboard.press("Enter");
  await pace(2000);

  // 6. Hold for clanker to work. The agent is live and nondeterministic; this
  //    pause just keeps the recording rolling so the response lands on tape.
  //    You'll trim the dead air (or narrate over it) in DaVinci. Bump
  //    KLANGK_DEMO_AGENT_WAIT to give it longer.
  const waitMs = Number(process.env.KLANGK_DEMO_AGENT_WAIT || 60_000);
  await pace(waitMs);

  // 7. Final beat — the created files, on the Chat tab.
  await pace(2000);
});
