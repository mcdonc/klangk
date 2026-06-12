const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;
const BRIDGE_TOKEN = process.env.KLANGK_BRIDGE_TOKEN;
const WORKSPACE_TOKEN = process.env.KLANGK_WORKSPACE_TOKEN;

export default function (pi: any) {
  if (!BRIDGE_URL || !BRIDGE_TOKEN) return;

  pi.registerTool({
    name: "itstime",
    description:
      "Play the 'it's time to stop' video overlay in the browser. " +
      "Use this when the user needs to stop what they're doing.",
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
            action: "itstime",
            token: BRIDGE_TOKEN,
          }),
        });

        if (resp.ok) {
          return {
            content: [{ type: "text", text: "It's time to stop." }],
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
            text: "Could not play video — no browser connected.",
          },
        ],
        details: {},
      };
    },
  });
}
