import { Type } from "@sinclair/typebox";

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
    name: "boing",
    description:
      "Display an Amiga-style bouncing ball animation overlay. " +
      "Only use this when the user explicitly asks for it.",
    parameters: Type.Object({
      text: Type.Optional(
        Type.String({
          description: "Text to display wrapped around the ball",
        }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { text?: string },
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      const payload: Record<string, string> = {
        action: "boing",
        browser_id: getBrowserId(),
      };
      if (params.text) {
        payload.text = params.text;
      }

      try {
        const resp = await fetch(`${BRIDGE_URL}/api/browser-delegate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(WORKSPACE_TOKEN
              ? { Authorization: `Bearer ${WORKSPACE_TOKEN}` }
              : {}),
          },
          body: JSON.stringify(payload),
        });

        if (resp.ok) {
          return {
            content: [
              {
                type: "text",
                text: `Boing ball displayed with text: "${params.text || "(default)"}"`,
              },
            ],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable — fall through to terminal
      }

      // Terminal fallback
      const text = params.text || "Klangk";
      process.stdout.write(`\n  \x1b[91m●\x1b[0m ${text}\n\n`);
      return {
        content: [{ type: "text", text: `Boing ball: "${text}"` }],
        details: {},
      };
    },
  });
}
