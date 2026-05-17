import { Type } from "@sinclair/typebox";

export default function (pi: any) {
  pi.registerTool({
    name: "celebrate",
    description:
      "Trigger a visual celebration with confetti in the user's browser. Use when the user asks to celebrate, or when a significant milestone is reached.",
    parameters: Type.Object({
      reason: Type.Optional(
        Type.String({ description: "What are we celebrating?" }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { reason?: string },
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      ctx: any,
    ) {
      const reason = params.reason || "Just because!";
      const request = JSON.stringify({ action: "celebrate", reason });
      const response = await ctx.ui.input("HOST_TOOL_REQUEST", request);
      return {
        content: [
          { type: "text", text: response || `🎉 Celebration! ${reason}` },
        ],
        details: {},
      };
    },
  });
}
