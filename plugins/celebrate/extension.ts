import { execSync } from "child_process";

function getBrowserId(): string {
  try {
    return execSync("klangk-browser-id", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;
const WORKSPACE_TOKEN = process.env.KLANGK_WORKSPACE_TOKEN;

const CONFETTI_CHARS = [
  "\x1b[91m*\x1b[0m",
  "\x1b[93m*\x1b[0m",
  "\x1b[92m*\x1b[0m",
  "\x1b[96m*\x1b[0m",
  "\x1b[95m*\x1b[0m",
  "\x1b[94m.\x1b[0m",
  "\x1b[91m'\x1b[0m",
  "\x1b[93m,\x1b[0m",
  "\x1b[92mo\x1b[0m",
];

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// Non-destructive terminal fallback for when the Klangk UI isn't connected.
// Renders in the ALTERNATE screen buffer with autowrap disabled, so it overlays
// without scrolling or clobbering the user's real terminal content; on exit the
// original screen is restored exactly.
async function ansiConfetti(): Promise<void> {
  const cols =
    process.stdout.columns ?? parseInt(process.env.COLUMNS || "80", 10);
  const rows = process.stdout.rows ?? parseInt(process.env.LINES || "24", 10);
  const particles: { x: number; y: number; ch: string; dy: number }[] = [];

  for (let i = 0; i < 40; i++) {
    particles.push({
      x: Math.floor(Math.random() * cols),
      y: 0,
      ch: CONFETTI_CHARS[Math.floor(Math.random() * CONFETTI_CHARS.length)],
      dy: 0.3 + Math.random() * 0.7,
    });
  }

  // Enter alt buffer, hide cursor, disable autowrap, clear.
  process.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J");
  try {
    for (let frame = 0; frame < 30; frame++) {
      // Clear previous positions
      for (const p of particles) {
        const row = Math.floor(p.y);
        if (row >= 1 && row <= rows) {
          process.stdout.write(`\x1b[${row};${p.x + 1}H `);
        }
      }
      // Update positions
      for (const p of particles) {
        p.y += p.dy;
        p.x += Math.random() < 0.5 ? -1 : 1;
        p.x = Math.max(0, Math.min(cols - 1, p.x));
      }
      // Draw new positions
      for (const p of particles) {
        const row = Math.floor(p.y);
        if (row >= 1 && row <= rows) {
          process.stdout.write(`\x1b[${row};${p.x + 1}H${p.ch}`);
        }
      }
      await sleep(80);
    }
  } finally {
    // Re-enable autowrap, show cursor, leave alt buffer (restores screen exactly).
    process.stdout.write("\x1b[?7h\x1b[?25h\x1b[?1049l");
  }
}

export default function (pi: any) {
  if (!BRIDGE_URL) return;

  pi.registerTool({
    name: "celebrate",
    description:
      "Celebrate with a confetti animation. " +
      "Use this when the user has accomplished something or asks you to celebrate.",
    parameters: {},
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      // Primary path: the Flutter ConfettiOverlay in the Klangk UI, triggered
      // through the browser bridge.
      try {
        const resp = await fetch(`${BRIDGE_URL}/api/browser-delegate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(WORKSPACE_TOKEN
              ? { Authorization: `Bearer ${WORKSPACE_TOKEN}` }
              : {}),
          },
          body: JSON.stringify({
            action: "celebrate",
            browser_id: getBrowserId(),
          }),
        });

        if (resp.ok) {
          return {
            content: [{ type: "text", text: "Celebration triggered!" }],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable — fall through to the non-destructive ANSI overlay.
      }

      // Fallback: alt-screen ANSI confetti (never scrolls/mangles the terminal).
      try {
        await ansiConfetti();
        return {
          content: [{ type: "text", text: "Celebration triggered!" }],
          details: {},
        };
      } catch {
        return {
          content: [{ type: "text", text: "Could not trigger celebration." }],
          details: {},
        };
      }
    },
  });
}
