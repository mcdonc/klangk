import { Type } from "@sinclair/typebox";

export default function (pi: any) {
  pi.registerTool({
    name: "beep",
    label: "Beep",
    description:
      "Play a beep sound in the user's browser. Use when the user asks for an audible alert, notification, or beep.",
    parameters: Type.Object({
      frequency: Type.Optional(
        Type.Number({ description: "Frequency in Hz (default 440)" }),
      ),
      duration: Type.Optional(
        Type.Number({ description: "Duration in milliseconds (default 200)" }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { frequency?: number; duration?: number },
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      ctx: any,
    ) {
      const freq = params.frequency || 440;
      const dur = params.duration || 600;
      const request = JSON.stringify({
        action: "beep",
        frequency: freq,
        duration: dur,
      });
      const response = await ctx.ui.input("HOST_TOOL_REQUEST", request);
      return {
        content: [
          { type: "text", text: response || `Beep! (${freq}Hz, ${dur}ms)` },
        ],
        details: {},
      };
    },
  });
}
