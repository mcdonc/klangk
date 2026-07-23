import { Type } from "@sinclair/typebox";

import { execSync } from "child_process";

function getBrowserId(): string {
  try {
    return execSync("klangk-browser-id", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

const BRIDGE_URL = process.env.KLANGKWS_BRIDGE_URL;
function getWorkspaceToken(): string {
  try {
    return execSync("klangk-workspace-token", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

export default function (pi: any) {
  if (!BRIDGE_URL) return;

  pi.registerTool({
    name: "boing",
    description:
      "Display a bouncing ball animation overlay. " +
      "Only use this when the user explicitly asks for it.",
    parameters: Type.Object({}),
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      const payload: Record<string, string> = {
        action: "boing",
        browser_id: getBrowserId(),
      };

      try {
        const token = getWorkspaceToken();
        const resp = await fetch(`${BRIDGE_URL}/api/v1/browser-delegate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify(payload),
        });

        if (resp.ok) {
          return {
            content: [{ type: "text", text: "Boing!" }],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable — fall through to terminal
      }

      process.stdout.write("\n  \x1b[91m●\x1b[0m Boing!\n\n");
      return {
        content: [{ type: "text", text: "Boing!" }],
        details: {},
      };
    },
  });
}
