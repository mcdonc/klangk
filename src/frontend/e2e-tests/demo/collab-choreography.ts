/**
 * Shared choreography for the two-scene collaboration arc (Scene 7 + 7b).
 *
 * The collaboration is told TWICE, once from each side (owner-recorded,
 * teammate-recorded), with the other party driven by a WebSocket sidechannel.
 * For the two recordings to intercut cleanly in the edit, the conversation —
 * its text, its speakers, its medium, and above all its CADENCE — must be
 * IDENTICAL across both. So it lives here, once, as a single `CONVERSATION`
 * array. Both scene files call `runConversation(ctx, perspective)`; the driver
 * performs each beat visibly (mouse + slowType on camera) when the beat's actor
 * is the recorded perspective, and as a sidechannel (WS terminal_input /
 * chat_send) otherwise.
 *
 * Public surface:
 *   - CONVERSATION            — the single source of truth (read/edit this)
 *   - setupCollab(opts)       — connect all 4 actor WS clients, open the
 *                               recorded browser page, resolve geometry
 *   - resetCollabState(ctx)   — unshare prior takes' terminals; leave chat
 *                               (lived-in); idempotent
 *   - runConversation(ctx, p) — walk CONVERSATION; visible vs sidechannel by p
 *   - teardownCollab(ctx)     — close WS clients
 *
 * FAST mode (KLANGK_DEMO_FAST=1): pace × 0.15, skip the live clanker wait, run
 * headless. Verifies every beat (visible + sidechannel) so you can iterate the
 * collaboration mechanics in ~10s without the LLM or a recording.
 */
import type { APIRequestContext, Page } from "@playwright/test";
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
  mouseClickRight,
  apiLogin,
  addRole,
  ensureSharedWorkspace,
  openWorkspaceDemo,
  openTab,
  openChatTab,
  openSharingTab,
  terminalTabCenterPx,
  connectWs,
  getMeId,
  captureContainerPane,
  type DemoWs,
} from "./demo-helpers";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Actor = "owner" | "teammate" | "designer" | "reviewer" | "clanker";
export type Medium = "system" | "terminal" | "chat";

/** One spoken/typed moment in the conversation. `text` and `afterPrevMs` are
 *  shared across BOTH perspectives (cadence/text can't drift). */
export type Beat = {
  /** Stable id — used in logs and (eventually) per-run message-id bookkeeping. */
  id: string;
  actor: Actor;
  medium: Medium;
  text: string;
  /** Pause BEFORE this beat, in ms. Same for both perspectives → intercut
   *  lines up. Scaled by FAST. */
  afterPrevMs: number;

  /** For the OWNER-only "share" system beat: which terminal tab to share. */
  shareTab?: "scratch" | "bash" | "terminal2";

  /** For chat beats typed visibly: slowType options. `submit:"enter"` presses
   *  Enter after typing (terminal beats always submit; chat beats that need a
   *  Send-button click use submit:"none" and the scene clicks Send). */
  type?: { cps?: number; trailing?: string };
  submit?: "enter" | "none";

  /** For the clanker beat: how long to wait for the live reply (skipped in
   *  FAST). */
  waitMs?: number;
};

/** A terminal tab's click target (focus coords) keyed by human name. */
type TabTarget = { x: number; y: number };

export type CollabCtx = {
  workspaceId: string;
  workspaceName: string;
  /** The RECORDED browser page (owner's in Sc 7, teammate's in Sc 7b). */
  page: Page;
  perspective: Actor;
  fast: boolean;
  /** Scale factor for all `pace()` calls. 1 normally, 0.15 in FAST. */
  scale: number;
  /** All four actor WS connections. The sidechannel driver sends through these
   *  by actor name; the recorded perspective's page also has its own implicit
   *  WS (the browser's), but sidechannel beats never use the recorded page. */
  ws: {
    owner: DemoWs;
    teammate: DemoWs;
    designer: DemoWs;
    reviewer: DemoWs;
  };
  /** User IDs (also = tmux session names). ownerUserId drives the share; the
   *  teammate join references ownerUserId + sharedWinId. */
  ownerUserId: string;
  teammateUserId: string;
  /** Filled when the owner shares (system "share" beat): the shared window id. */
  sharedWinId?: string;
  /** Geometry resolved from the recorded page's viewport. */
  tab: {
    scratch: TabTarget;
    bash: TabTarget;
    shared: TabTarget; // where a joined shared terminal tab sits for the teammate
  };
  chatBox: { x: number; y: number };
  /** Names of the owning terminal windows, for capture/verify. */
  scratchWindowName: string;
};

// ---------------------------------------------------------------------------
// The conversation — the single source of truth. Edit HERE, both scenes move.
// ---------------------------------------------------------------------------

export const CONVERSATION: Beat[] = [
  // --- setup beat: owner clicks the Sharing nav tab and holds while the VO
  //     describes the four roles (Owners / Coders / Collaborators /
  //     Spectators). The teammate is already listed as a Collaborator
  //     (setupCollab granted the role pre-roll). Teammate perspective: no-op
  //     (collaborators have no Sharing tab — only Terminal/Files/Chat). ---
  {
    id: "sharing-tour",
    actor: "owner",
    medium: "system",
    text: "",
    afterPrevMs: 0,
  },

  // --- setup beat: owner shares the scratch terminal. Not spoken; visible as
  //     a click on the scratch tab's share toggle (owner perspective) or a WS
  //     share_window sidechannel (teammate perspective, so the shared tab
  //     appears on camera when the teammate's recording starts). ---
  {
    id: "share",
    actor: "owner",
    medium: "system",
    text: "",
    afterPrevMs: 0,
    shareTab: "scratch",
  },

  // --- pair programming in the shared terminal ---
  {
    id: "owner-typ",
    actor: "owner",
    medium: "terminal",
    text: "echo 'owner typing here'",
    afterPrevMs: 2000,
    submit: "enter",
  },
  {
    id: "tm-typ",
    actor: "teammate",
    medium: "terminal",
    text: "echo 'teammate typing back'",
    afterPrevMs: 2500,
    submit: "enter",
  },

  // --- move to chat ---
  {
    id: "designer-1",
    actor: "designer",
    medium: "chat",
    text: "hey, can we add a simple landing page?",
    afterPrevMs: 2000,
  },
  {
    id: "reviewer-1",
    actor: "reviewer",
    medium: "chat",
    text: "yeah — minimal, just a headline and a button",
    afterPrevMs: 2500,
  },
  {
    id: "owner-clanker",
    actor: "owner",
    medium: "chat",
    text: "@clanker scaffold a simple Flask landing page with a headline and a button",
    afterPrevMs: 2500,
    type: { cps: 14 },
    // Visible chat beats are submitted with a mouse click on Send (see scene),
    // so the driver does not press Enter; the scene/performVisible handles it.
    submit: "none",
  },

  // --- the clanker reply is special: live LLM, never sidechanneled ---
  {
    id: "clanker-reply",
    actor: "clanker",
    medium: "chat",
    text: "",
    afterPrevMs: 1000,
    waitMs: Number(process.env.KLANGK_DEMO_AGENT_WAIT || 60_000),
  },

  {
    id: "tm-react",
    actor: "teammate",
    medium: "chat",
    text: "nice — let's wire that button up next",
    afterPrevMs: 3000,
  },
];

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

type SetupOpts = {
  page: Page;
  request: APIRequestContext;
  perspective: Actor;
};

/** Set up the collaboration: ensure workspace + roles, connect all four actor
 *  WS clients, open the recorded browser page, resolve geometry. Returns a
 *  context for runConversation.
 *
 *  For the TEAMMATE perspective, the owner is expected to have ALREADY shared
 *  the scratch terminal (the owner's recording, or a fresh share done here via
 *  the owner WS). We do the share here as part of the conversation's "share"
 *  beat, so both perspectives are self-contained. */
export async function setupCollab({
  page,
  request,
  perspective,
}: SetupOpts): Promise<CollabCtx> {
  const fast = process.env.KLANGK_DEMO_FAST === "1";
  const scale = fast ? 0.15 : 1;

  // --- workspace + roles (idempotent) ---
  const { headers: ownerHeaders, token: ownerToken } = await apiLogin(
    request,
    DEMO_HERO_EMAIL,
    DEMO_HERO_PASSWORD,
  );
  const ws = await ensureSharedWorkspace(
    request,
    ownerHeaders,
    SHARED_WORKSPACE,
  );
  for (const email of [
    DEMO_TEAMMATE_EMAIL,
    DEMO_DESIGNER_EMAIL,
    DEMO_REVIEWER_EMAIL,
  ]) {
    await addRole(request, ownerHeaders, ws.id, "collaborators", email);
  }

  // --- user ids (= tmux session names for terminal capture) ---
  const ownerUserId = await getMeId(request, ownerHeaders);
  const { token: teammateToken, headers: teammateHeaders } = await apiLogin(
    request,
    DEMO_TEAMMATE_EMAIL,
    DEMO_PASSWORD,
  );
  const teammateUserId = await getMeId(request, teammateHeaders);

  // --- open the RECORDED browser page as the perspective ---
  if (perspective === "owner") {
    await openWorkspaceDemo(page, DEMO_HERO_EMAIL, ws.id, DEMO_HERO_PASSWORD);
  } else {
    // Teammate: container already up (owner opened it); attach without waiting
    // for container_ready.
    await openWorkspaceDemo(page, DEMO_TEAMMATE_EMAIL, ws.id, DEMO_PASSWORD, {
      waitForContainer: false,
    });
  }
  await pace(1500 * scale);

  // --- connect all four actor WS clients (sidechannel drivers + the
  //     owner-side share). The recorded perspective's own browser WS is
  //     separate; we don't touch it here. ---
  const [ownerWs, teammateWs, designerWs, reviewerWs] = await Promise.all([
    connectWs(ownerToken, ws.id),
    connectWs(teammateToken, ws.id),
    connectWs(
      (await apiLogin(request, DEMO_DESIGNER_EMAIL, DEMO_PASSWORD)).token,
      ws.id,
    ),
    connectWs(
      (await apiLogin(request, DEMO_REVIEWER_EMAIL, DEMO_PASSWORD)).token,
      ws.id,
    ),
  ]);

  // --- find the scratch window name. CRITICAL: only enumerate via ownerWs
  //     for the TEAMMATE perspective (no owner browser). For the OWNER
  //     perspective, the owner's browser is the recorded window and the SOLE
  //     driver of the owner's terminal — calling terminal_start on a second
  //     owner WS connection reassigns window ids server-side, so the browser's
  //     _selectedOwnWindowId falls back to windows[0] (bash) and the "scratch"
  //     sub-tab then renders bash/pi content (verified: browser-only targets
  //     scratch correctly; the sidechannel enumeration is what desyncs). So for
  //     the owner we use the known continuity name and discover the id later
  //     from the shared_terminals broadcast.
  let scratchWindowName = "scratch";
  if (perspective === "teammate") {
    ownerWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
    const winMsg = await ownerWs.recvUntil(
      (m) => m.type === "terminal_windows",
    );
    const allWindows = (winMsg.windows as Array<Record<string, unknown>>) ?? [];
    const scratchWin =
      allWindows.find((w) => w.name === "scratch") ??
      allWindows.find((w) => w.name === "terminal2") ??
      allWindows[allWindows.length - 1];
    scratchWindowName = String(scratchWin?.name ?? "scratch");
  }

  // --- geometry from the recorded viewport ---
  const { width, height } = vp(page);
  const tabY = height * 0.2; // terminal sub-tab strip vertical center
  const ctx: CollabCtx = {
    workspaceId: ws.id,
    workspaceName: ws.name,
    page,
    perspective,
    fast,
    scale,
    ws: {
      owner: ownerWs,
      teammate: teammateWs,
      designer: designerWs,
      reviewer: reviewerWs,
    },
    ownerUserId,
    teammateUserId,
    tab: {
      // bash=0, terminal2=1, scratch=2 (demo workspace from Sc 2/4/5b)
      bash: { x: terminalTabCenterPx(0), y: tabY },
      terminal2: { x: terminalTabCenterPx(1), y: tabY },
      scratch: { x: terminalTabCenterPx(2), y: tabY },
      // The teammate's joined shared terminal shows up as a tab; its index
      // depends on how many own-tabs the teammate has. For a fresh teammate
      // with one own bash tab, the shared tab is at index 1. Calibrated below
      // at use time; default to index 1.
      shared: { x: terminalTabCenterPx(1), y: tabY },
    },
    chatBox: { x: width / 2, y: height - 25 },
    scratchWindowName,
  };
  return ctx;
}

// ---------------------------------------------------------------------------
// Reset (idempotent, run at the top of every take)
// ---------------------------------------------------------------------------

/** Reset collaboration state between takes. Unshares any prior terminal shares
 *  (queried live), and clears the scratch pane so the echo lines don't pile
 *  up. Chat history is LEFT ACCUMULATED — it reads as "lived-in" (prior
 *  designer/reviewer discussion) and there's no bulk-clear API; per-message
 *  author-only soft-delete isn't worth the bookkeeping. */
export async function resetCollabState(ctx: CollabCtx): Promise<void> {
  const { ws, page, perspective, scale } = ctx;

  // Unshare whatever the owner is currently sharing (idempotent: no-op if
  // nothing's shared). Driven over the owner WS regardless of perspective,
  // since only the owner holds the share.
  try {
    const shared = await ws.owner.recvUntil(
      (m) => m.type === "shared_terminals" && Array.isArray(m.terminals),
      5_000,
    );
    const terminals =
      (shared.terminals as Array<Record<string, unknown>>) ?? [];
    for (const t of terminals) {
      const userId = t.user_id as string | undefined;
      const winId = t.window_id as string | undefined;
      if (userId && winId) {
        ws.owner.send({
          cmd: "unshare_window",
          user_id: userId,
          window_id: winId,
        });
      }
    }
  } catch {
    // No shared_terminals message in flight — nothing to unshare.
  }

  // NOTE: we do NOT clear the scratch pane via the owner WS sidechannel. The
  // recorded user's browser is the SOLE driver of their terminal — sending
  // terminal_select_window/terminal_input over a second WS connection for the
  // same user desyncs the browser's active-window tracking (keystrokes then
  // land in the wrong window). Stale echo lines from prior takes are cosmetic
  // and don't affect substring verification; clear off-camera before a real
  // recording if needed. Workspace-level ops (share/unshare) above are safe —
  // they don't touch per-session active-window state.

  // Settle so the unshare propagates before the conversation starts.
  await pace(1000 * scale);
}

// ---------------------------------------------------------------------------
// The driver
// ---------------------------------------------------------------------------

/** Walk CONVERSATION, performing each beat visibly (recorded perspective) or
 *  as a sidechannel (every other actor). Verifies each beat's effect and logs
 *  PASS/FAIL. In FAST mode a failed verification throws (fast feedback); in a
 *  real recording it only logs (so a flaky beat doesn't abort the take). */
export async function runConversation(
  ctx: CollabCtx,
  perspective: Actor,
): Promise<void> {
  for (const beat of CONVERSATION) {
    if (beat.afterPrevMs) await pace(beat.afterPrevMs * ctx.scale);
    console.log(
      `[beat] start ${beat.id} (actor=${beat.actor}, medium=${beat.medium})`,
    );

    if (beat.actor === "clanker") {
      await waitForClanker(ctx, beat);
      continue;
    }

    if (beat.id === "sharing-tour") {
      await performSharingTour(ctx, perspective);
      continue;
    }

    if (beat.id === "share") {
      await performShare(ctx, perspective);
      continue;
    }

    // The teammate must join the shared terminal before any terminal beat
    // (their side) or before the owner's terminal beat (so the join is in
    // place). Done lazily once.
    if (beat.medium === "terminal" && !ctx.sharedWinId) {
      // share beat should have set it; guard anyway
    }

    if (beat.actor === perspective) {
      await performVisible(ctx, beat);
    } else {
      await performSidechannel(ctx, beat);
    }

    await verifyBeat(ctx, beat);
  }
}

// --- special beats ---------------------------------------------------------

/** The clanker reply is a live LLM call, never sidechanneled. In FAST mode we
 *  skip the wait (mechanics only). Otherwise poll the chat for a new agent
 *  message and leave dead air for VO. */
async function waitForClanker(ctx: CollabCtx, beat: Beat): Promise<void> {
  if (ctx.fast) {
    console.log(`[beat] SKIP ${beat.id} (FAST: no live clanker)`);
    return;
  }
  console.log(`[beat] WAIT ${beat.id} (live clanker, up to ${beat.waitMs}ms)`);
  // The agent's reply arrives as a chat_message from the agent user over the
  // owner WS (and broadcasts). Poll any connection; we don't assert content
  // (nondeterministic), just that a new agent message lands within the window.
  const deadline = Date.now() + (beat.waitMs ?? 60_000);
  // Drain sidechannel clients so they don't buffer; the agent message will
  // appear on whichever WS we read. Use the designer WS (a passive listener).
  try {
    await ctx.ws.designer.recvUntil(
      (m) =>
        m.type === "chat_message" &&
        (m as { user_email?: string }).user_email?.includes("clanker"),
      Math.max(2_000, deadline - Date.now()),
    );
    console.log(`[beat] PASS ${beat.id} (clanker replied)`);
  } catch {
    console.log(`[beat] WARN ${beat.id} (no clanker reply within window)`);
  }
}

/** The "sharing-tour" system beat. Owner perspective: click the Sharing nav
 *  tab (index 3 of 5) and hold while the VO describes the four roles and notes
 *  the teammate is already a Collaborator. Then click back to the Terminal nav
 *  tab so the subsequent share beat's scratch-tab click lands in the terminal
 *  area. Teammate perspective: no-op (collaborators have no Sharing tab — only
 *  Terminal/Files/Chat). */
async function performSharingTour(
  ctx: CollabCtx,
  perspective: Actor,
): Promise<void> {
  if (perspective !== "owner") {
    console.log(`[beat] SKIP sharing-tour (${perspective}: no Sharing tab)`);
    return;
  }
  const { page, scale } = ctx;
  // Sharing nav tab = index 3 of 5 (Terminal/Files/Chat/Sharing/Settings).
  await openTab(page, 3, 5);
  await pace(8000 * scale); // VO: describe roles, note teammate is a Collaborator
  // Return to the Terminal nav tab. Clicking a nav tab swaps the shown pane;
  // on returning to Terminal the pane re-mounts and may default to the bash
  // sub-tab, so re-click the scratch sub-tab to re-establish it as the active
  // terminal before the share beat runs (the share beat clicks scratch's
  // share toggle and the owner types there). The settle lets the remount
  // settle so the sub-tab click lands on the terminal strip, not Sharing UI.
  await openTab(page, 0, 5);
  await pace(1500 * scale);
  await mouseClick(page, ctx.tab.scratch.x, ctx.tab.scratch.y);
  await pace(1000 * scale);
  console.log(`[beat] PASS sharing-tour (Sharing panel shown)`);
}

/** The "share" system beat.
 *
 *  Owner perspective: the share is performed ON CAMERA via the browser —
 *  right-click the scratch sub-tab → pick "Share" from the context menu → a
 *  share badge (cell-tower icon) appears on the tab. The owner's browser is
 *  the SOLE driver of the owner's terminal, so we send NO terminal op over
 *  the owner WS sidechannel (terminal_start reassigns window ids server-side
 *  and desyncs the browser's sub-tab→window mapping; share_window would
 *  short-circuit the on-camera action). We learn the shared window id from
 *  the shared_terminals broadcast on a passive observer (designer WS).
 *
 *  Teammate perspective: there is no owner browser, so the owner WS
 *  sidechannel IS the sole owner connection — safe to enumerate + share
 *  there. Then the teammate joins the shared terminal. */
async function performShare(ctx: CollabCtx, perspective: Actor): Promise<void> {
  const { ws, page } = ctx;

  if (perspective === "owner") {
    // VISIBLE: right-click the scratch sub-tab → context menu → click Share.
    // The menu renders at the click point; Share is row 2 (Rename, Share),
    // each 48px → Share center = click point + (+56, +80) (measured via
    // Flutter semantics). A share badge (cell-tower icon) appears on the tab.
    const target = ctx.tab.scratch;
    await mouseClick(page, target.x, target.y); // focus scratch first
    await pace(800 * ctx.scale);
    await mouseClickRight(page, target.x, target.y); // right-click → menu
    await pace(900 * ctx.scale); // menu appears
    await mouseClick(page, target.x + 56, target.y + 80); // Share menu item
    await pace(1200 * ctx.scale); // share badge appears on the tab
  } else {
    // Teammate perspective: no owner browser — enumerate + share via ownerWs
    // (the sole owner connection; safe).
    ws.owner.send({ cmd: "terminal_start", cols: 80, rows: 24 });
    const winMsg = await ws.owner.recvUntil(
      (m) => m.type === "terminal_windows",
    );
    const allWindows = (winMsg.windows as Array<Record<string, unknown>>) ?? [];
    const scratchWin =
      allWindows.find((w) => w.name === ctx.scratchWindowName) ??
      allWindows[allWindows.length - 1];
    ws.owner.send({
      cmd: "share_window",
      window_id: String(scratchWin.id),
    });
  }

  // Discover the shared window id from the shared_terminals broadcast on a
  // passive observer (designer WS). Avoids any ownerWs terminal op for the
  // owner perspective and works for both perspectives.
  try {
    const shared = await ws.designer.recvUntil(
      (m) =>
        m.type === "shared_terminals" &&
        ((m.terminals as Array<Record<string, unknown>>) ?? []).length > 0,
      10_000,
    );
    const terms = (shared.terminals as Array<Record<string, unknown>>) ?? [];
    const term = terms[0];
    if (term?.window_id) ctx.sharedWinId = String(term.window_id);
  } catch {
    // broadcast may have been drained; sharedWinId stays undefined (lazy
    // discovery in teammateJoinShared covers it)
  }
  await pace(1500 * ctx.scale);

  // Teammate joins the shared terminal now (teammate perspective only). For
  // the owner perspective, the teammate sidechannel join happens lazily on the
  // first teammate terminal beat.
  if (perspective === "teammate") {
    await teammateJoinShared(ctx);
  }

  console.log(
    `[beat] PASS share (shared ${ctx.scratchWindowName}, win=${ctx.sharedWinId ?? "?"})`,
  );
}

/** Teammate joins the owner's shared terminal over the teammate WS. The
 *  teammate must terminal_start first to become a terminal subscriber that
 *  receives shared_terminals broadcasts (and to have a session to join into). */
async function teammateJoinShared(ctx: CollabCtx): Promise<void> {
  // Establish the teammate's terminal session (needed before join), but do
  // NOT wait for its terminal_started response by scanning the queue — that
  // would consume the shared_terminals broadcast (queued earlier, at the
  // front) which we need next. The shared_terminals from the owner's share is
  // already in the teammate's queue; recvUntil finds it directly.
  ctx.ws.teammate.send({ cmd: "terminal_start", cols: 80, rows: 24 });
  await pace(500 * ctx.scale);
  const shared = await ctx.ws.teammate.recvUntil(
    (m) =>
      m.type === "shared_terminals" &&
      ((m.terminals as Array<Record<string, unknown>>) ?? []).length > 0,
    10_000,
  );
  const terms = (shared.terminals as Array<Record<string, unknown>>) ?? [];
  const term = terms[0];
  ctx.ws.teammate.send({
    cmd: "join_shared_terminal",
    user_id: term.user_id as string,
    window_id: term.window_id as string,
  });
  try {
    await ctx.ws.teammate.recvUntil(
      (m) => m.type === "terminal_started",
      10_000,
    );
  } catch {
    // some flows don't re-emit terminal_started on join
  }
  await pace(1500 * ctx.scale);
  console.log(`[beat] teammate joined shared terminal`);
}

// --- visible performance (recorded perspective) ---------------------------

async function performVisible(ctx: CollabCtx, beat: Beat): Promise<void> {
  const { page, scale } = ctx;
  if (beat.medium === "terminal") {
    // Focus the relevant tab: owner types into the shared terminal (the
    // owner's own scratch tab, which is shared); teammate types into the
    // joined shared tab. Click to focus, then type.
    const target =
      ctx.perspective === "owner" ? ctx.tab.scratch : ctx.tab.shared;
    await mouseClick(page, target.x, target.y);
    await pace(800 * scale);
    await slowType(page, beat.text, beat.type ?? {});
    if (beat.submit === "enter" || beat.submit === undefined) {
      await page.keyboard.press("Enter");
    }
    await pace(500 * scale);
    return;
  }
  if (beat.medium === "chat") {
    // Ensure the Chat tab is open. Tab count is permission-gated: owner has
    // 5 nav tabs, teammate (collaborator role) has 3 (Terminal/Files/Chat).
    const navTabCount = ctx.perspective === "owner" ? 5 : 3;
    await openChatTab(page, navTabCount);
    await pace(600 * scale);
    // Focus the chat input box.
    await mouseClick(page, ctx.chatBox.x, ctx.chatBox.y);
    await pace(400 * scale);
    // Triple-click to clear any prior draft, then type.
    await page.mouse.click(ctx.chatBox.x, ctx.chatBox.y, { clickCount: 3 });
    await pace(150 * scale);
    const text = beat.type?.trailing
      ? `${beat.text}${beat.type.trailing}`
      : beat.text;
    await slowType(page, text, beat.type ?? {});
    // For chat, submit by clicking the Send button (NOT Enter) per the
    // interaction rules. The Send button sits just right of the input box.
    await pace(400 * scale);
    const { width } = vp(page);
    await mouseClick(page, width * 0.95, ctx.chatBox.y);
    await pace(500 * scale);
    return;
  }
}

// --- sidechannel performance (every actor but the recorded perspective) ----

async function performSidechannel(ctx: CollabCtx, beat: Beat): Promise<void> {
  const conn =
    ctx.ws[beat.actor as "owner" | "teammate" | "designer" | "reviewer"];
  if (beat.medium === "terminal") {
    // A teammate sidechannel terminal beat: the teammate must already be
    // joined to the shared terminal. Ensure that (lazy, once).
    if (beat.actor === "teammate" && ctx.perspective === "owner") {
      // Owner perspective: the teammate isn't joined yet (only the owner's
      // recording is live). Join now so terminal_input lands in the shared pty.
      await teammateJoinShared(ctx);
    }
    conn.send({ cmd: "terminal_input", data: `${beat.text}\r` });
    return;
  }
  if (beat.medium === "chat") {
    conn.send({ cmd: "chat_send", message: beat.text });
    return;
  }
}

// --- verification ----------------------------------------------------------

/** Verify the beat's effect landed. Perspective-independent: checks the
 *  EFFECT (text in the shared pane, chat message echoed), so a FAST owner
 *  dry-run also exercises the teammate sidechannel write. */
async function verifyBeat(ctx: CollabCtx, beat: Beat): Promise<void> {
  if (beat.medium === "terminal" && beat.text) {
    // The shared terminal is the owner's scratch window (ownerUserId session).
    // Wait for the typed text to appear in the pane. The echo includes the
    // typed text, so substring-match it.
    try {
      await waitForPaneTextShallow(ctx, beat.text, 8_000);
      console.log(`[beat] PASS ${beat.id} (terminal text landed)`);
      return;
    } catch {
      return fail(ctx, beat, "terminal text not seen in shared pane");
    }
  }
  if (beat.medium === "chat" && beat.text) {
    // The chat message echoes back as chat_message on all WS clients. Wait for
    // it on a passive listener (designer).
    try {
      await ctx.ws.designer.recvUntil(
        (m) =>
          m.type === "chat_message" &&
          (m as { message?: string }).message === beat.text,
        8_000,
      );
      console.log(`[beat] PASS ${beat.id} (chat echoed)`);
      return;
    } catch {
      return fail(ctx, beat, "chat message not echoed");
    }
  }
}

function fail(ctx: CollabCtx, beat: Beat, why: string): void {
  const msg = `[beat] FAIL ${beat.id}: ${why}`;
  // Dump a screenshot so we can see what's on screen at the failure.
  try {
    const shot = `/tmp/collab-debug-${beat.id}-${ctx.perspective}.png`;
    void ctx.page
      .screenshot({ path: shot })
      .then(() => console.log(`[beat]   debug screenshot: ${shot}`));
  } catch {
    // best effort
  }
  if (ctx.fast) throw new Error(msg);
  console.log(msg);
}

/** A shallow (short-timeout, short-scrollback) variant of waitForPaneText for
 *  per-beat verification. Reads the owner's scratch pane via the container. */
async function waitForPaneTextShallow(
  ctx: CollabCtx,
  needle: string,
  timeoutMs: number,
): Promise<void> {
  // The shared terminal's window name is ctx.scratchWindowName in the owner's
  // tmux session (ownerUserId). captureContainerPane takes (workspace, session,
  // window, lines) where window is an int index — but our window is named, not
  // necessarily index 2. Use the bash helper directly via klangkcExec through
  // the owner WS? Simpler: capture by session+window-name using tmux's name
  // form. We approximate by capturing the owner session across a few windows.
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      // Try the named window directly via klangkcExec (captureContainerPane
      // only takes an int window; replicate its tmux call with a window name).
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      const { execFileSync } = require("node:child_process") as {
        execFileSync: (c: string, a: string[], o: object) => string;
      };
      const server = process.env.KLANGK_TEST_URL || "http://localhost:8996";
      const pane = execFileSync(
        "klangkc",
        [
          "--server",
          server,
          "exec",
          ctx.workspaceName,
          "bash",
          "-lc",
          `tmux capture-pane -t ${ctx.ownerUserId}:${ctx.scratchWindowName} -p -S -12`,
        ],
        { encoding: "utf-8", timeout: 30_000 },
      );
      // Suppress unused-server lint: server is used above.
      void server;
      if (pane.includes(needle)) return;
    } catch {
      // window may not resolve by name on every poll; keep trying
    }
    await pace(1_000);
  }
  throw new Error(`waitForPaneTextShallow: ${needle} not seen`);
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

export async function teardownCollab(ctx: CollabCtx): Promise<void> {
  for (const c of Object.values(ctx.ws)) {
    try {
      c.close();
    } catch {
      // best effort
    }
  }
}
