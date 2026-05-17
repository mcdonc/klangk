import { Type } from "@sinclair/typebox";

export default function (pi: any) {
  pi.registerTool({
    name: "word_count",
    label: "Word Count",
    description:
      "Fast file analysis: returns line count, word count, character count, and file size. Use this instead of reading entire files when you just need basic stats.",
    parameters: Type.Object({
      path: Type.String({ description: "Path to the file to analyze" }),
    }),
    async execute(
      _toolCallId: string,
      params: { path: string },
      signal: AbortSignal,
    ) {
      try {
        const result = await pi.exec(
          "python3",
          ["/usr/local/bin/bark-tools/word_count.py", params.path],
          { signal, timeout: 10000 },
        );
        return {
          content: [{ type: "text", text: result.stdout || result.stderr }],
          details: {},
        };
      } catch (err: any) {
        return {
          content: [{ type: "text", text: `Error: ${err.message}` }],
          details: {},
        };
      }
    },
  });
}
