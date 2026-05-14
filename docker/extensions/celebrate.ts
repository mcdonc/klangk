import { Type } from "@sinclair/typebox";

export default function (pi: any) {
  pi.registerTool({
    name: "celebrate",
    description: "Trigger a visual celebration with confetti. Use when the user asks to celebrate, or when a significant milestone is reached.",
    parameters: Type.Object({
      reason: Type.Optional(Type.String({ description: "What are we celebrating?" })),
    }),
    async execute(_toolCallId: string, params: { reason?: string }) {
      const reason = params.reason || "Just because!";
      return {
        content: [{ type: "text", text: `🎉 Celebration! ${reason}` }],
        details: {},
      };
    },
  });
}
