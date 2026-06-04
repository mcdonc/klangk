// Pi extension for the xeyes overlay. Mirrors the celebrate plugin: it talks
// to the Flutter client through the browser-delegate bridge. Exposes both a
// user slash command (`/xeyes [on|off]`) and an LLM-callable tool (`xeyes`),
// each POSTing `{action: "xeyes", token, on?}` to the backend bridge, which
// relays it to the Klangk UI where XeyesPlugin toggles the eyes.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;
const BRIDGE_TOKEN = process.env.KLANGK_BRIDGE_TOKEN;

async function toggleXeyes(on?: boolean): Promise<boolean> {
  if (!BRIDGE_URL || !BRIDGE_TOKEN) return false;
  try {
    const resp = await fetch(`${BRIDGE_URL}/api/browser-delegate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "xeyes",
        token: BRIDGE_TOKEN,
        ...(on === undefined ? {} : { on }),
      }),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

function parseOnOff(args: string): boolean | undefined {
  const a = args.trim().toLowerCase();
  if (a === "on") return true;
  if (a === "off") return false;
  return undefined;
}

export default function (pi: ExtensionAPI) {
  if (!BRIDGE_URL || !BRIDGE_TOKEN) return;

  // /xeyes [on|off] — slash command typed by the user in the Pi REPL.
  // Guarded in case the running pi predates registerCommand.
  const api = pi as unknown as {
    registerCommand?: (
      name: string,
      options: {
        description?: string;
        handler: (args: string, ctx: unknown) => Promise<void>;
      },
    ) => void;
  };
  if (typeof api.registerCommand === "function") {
    api.registerCommand("xeyes", {
      description: "Toggle the googly-eyes overlay in the Klangk UI ([on|off])",
      handler: async (args: string) => {
        await toggleXeyes(parseOnOff(args));
      },
    });
  }

  // Also expose as an LLM tool so the agent can toggle it on request.
  pi.registerTool({
    name: "xeyes",
    description:
      "Toggle the googly-eyes (xeyes) overlay in the Klangk UI on or off.",
    parameters: {},
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: unknown,
      _ctx: unknown,
    ) {
      const ok = await toggleXeyes();
      return {
        content: [
          {
            type: "text",
            text: ok
              ? "Toggled the xeyes overlay."
              : "xeyes overlay unavailable (no Klangk UI connected).",
          },
        ],
        details: {},
      };
    },
  });
}
