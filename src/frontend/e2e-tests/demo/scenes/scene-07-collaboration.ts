/**
 * Scene 7 — Collaboration: The Owner's View (~1.5–2 min)
 *
 * One of TWO mirrored recordings of the same conversation (see
 * collab-choreography.ts). This scene records the OWNER's single browser
 * window; the teammate/designer/reviewer halves are driven by WebSocket
 * sidechannels. The other half is Scene 7b (scene-07b-collaboration-teammate.ts,
 * the teammate's window). The two clips intercut in the edit (DaVinci).
 *
 * The conversation itself — text, speakers, medium, cadence — lives ONCE in
 * CONVERSATION (collab-choreography.ts). This file is a thin wrapper: set up,
 * reset, run, teardown.
 *
 * Iterate the mechanics in FAST mode (headless, ~10s, no live clanker):
 *   KLANGK_DEMO_FAST=1 KLANGK_DEMO_HEADLESS=1 \
 *     devenv shell -- npx playwright test --config=...demo.config.ts \
 *       -g "collaboration owner"
 * Record for real:
 *   devenv shell -- src/frontend/e2e-tests/demo/record-demo.sh \
 *     -g "collaboration owner"
 */
import { test } from "@playwright/test";
import {
  setupCollab,
  resetCollabState,
  runConversation,
  teardownCollab,
} from "../collab-choreography";

test("collaboration owner", async ({ page, request }) => {
  test.setTimeout(process.env.KLANGK_DEMO_FAST ? 120_000 : 300_000);

  const ctx = await setupCollab({ page, request, perspective: "owner" });
  try {
    await resetCollabState(ctx);
    await runConversation(ctx, "owner");
  } finally {
    await teardownCollab(ctx);
  }
});
