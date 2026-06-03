const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;
const BRIDGE_TOKEN = process.env.KLANGK_BRIDGE_TOKEN;

export default function (pi: any) {
  if (!BRIDGE_URL || !BRIDGE_TOKEN) return;

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
        const resp = await fetch(`${BRIDGE_URL}/api/browser-delegate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "bobdobbs",
            token: BRIDGE_TOKEN,
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
