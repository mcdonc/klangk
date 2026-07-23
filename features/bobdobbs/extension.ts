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
    name: "bobdobbs",
    description:
      'Show a rotating J.R. "Bob" Dobbs head. ' +
      "Use this when the user needs more Slack in their life, " +
      "or when they ask about the Church of the SubGenius.",
    parameters: {},
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      try {
        const token = getWorkspaceToken();
        const resp = await fetch(`${BRIDGE_URL}/api/v1/browser-delegate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            action: "bobdobbs",
            browser_id: getBrowserId(),
          }),
        });

        if (resp.ok) {
          return {
            content: [
              { type: "text", text: '"Bob" has blessed this workspace.' },
            ],
            details: {},
          };
        }
      } catch {
        // Bridge unreachable
      }

      return {
        content: [
          {
            type: "text",
            text: '"Bob" tried to appear but no browser was connected.',
          },
        ],
        details: {},
      };
    },
  });
}
