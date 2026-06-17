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
    name: "marquee",
    description:
      "Display a flashy scrolling marquee banner with rainbow animations. " +
      "Only use this when the user explicitly asks for a marquee or banner. " +
      "Never use this for greetings or unprompted messages.",
    parameters: Type.Object({
      text: Type.Optional(
        Type.String({
          description:
            "Text to display (uses configured default if not provided)",
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
        action: "marquee",
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
                text: `Marquee displayed: "${params.text || "(default)"}"`,
              },
            ],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable — fall through to terminal
      }

      // Terminal fallback: print with some ANSI flair
      const text = params.text || "Hello from Klangk!";
      const colors = [
        "\x1b[91m",
        "\x1b[93m",
        "\x1b[92m",
        "\x1b[96m",
        "\x1b[95m",
        "\x1b[94m",
      ];
      const rainbow = text
        .split("")
        .map((ch, i) => `${colors[i % colors.length]}${ch}`)
        .join("");
      process.stdout.write(`\n  ${rainbow}\x1b[0m\n\n`);
      return {
        content: [{ type: "text", text: `Marquee displayed: "${text}"` }],
        details: {},
      };
    },
  });
}
