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

export default function (pi: any) {
  if (!BRIDGE_URL) return;

  pi.registerTool({
    name: "beep",
    description:
      "Play a beep sound to get the user's attention or signal completion.",
    parameters: {},
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
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
            action: "beep",
            browser_id: getBrowserId(),
          }),
        });

        if (resp.ok) {
          return {
            content: [{ type: "text", text: "Beep played." }],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable — fall through to terminal bell
      }

      // No browser connected or bridge failed — terminal bell fallback
      process.stdout.write("\x07");
      return {
        content: [{ type: "text", text: "Beep played." }],
        details: {},
      };
    },
  });
}
