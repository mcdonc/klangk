/**
 * Scene 7b — Collaboration: The Teammate's View (~1.5–2 min)
 *
 * The mirror of Scene 7 (scene-08-collaboration.ts). Records the TEAMMATE's
 * single browser window; the owner/designer/reviewer halves are driven by
 * WebSocket sidechannels. Same CONVERSATION (collab-choreography.ts) as Sc 7,
 * performed from the teammate's perspective. Intercut against Sc 7 in the edit.
 *
 * The teammate is a Collaborator → 3 nav tabs (Terminal/Files/Chat; no
 * Sharing, no Settings). That is correct, not a bug.
 *
 * Iterate in FAST mode:
 *   KLANGK_DEMO_FAST=1 KLANGK_DEMO_HEADLESS=1 \
 *     devenv shell -- npx playwright test --config=...demo.config.ts \
 *       -g "collaboration teammate"
 */
import { test } from "@playwright/test";
import {
  setupCollab,
  resetCollabState,
  runConversation,
  teardownCollab,
} from "../collab-choreography";

test("collaboration teammate", async ({ page, request }) => {
  test.setTimeout(process.env.KLANGK_DEMO_FAST ? 120_000 : 300_000);

  const ctx = await setupCollab({ page, request, perspective: "teammate" });
  try {
    await resetCollabState(ctx);
    await runConversation(ctx, "teammate");
  } finally {
    await teardownCollab(ctx);
  }
});
