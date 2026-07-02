import { Type } from "@sinclair/typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export default function (pi: any) {
  pi.registerTool({
    name: "get_hosted_url",
    description:
      "Get the hosted URL for a web app running on a container port. " +
      "Returns the full URL the user should visit in their browser.",
    parameters: Type.Object({
      container_port: Type.Number({
        description: "The port number inside the container",
      }),
    }),
    async execute(
      _toolCallId: string,
      params: { container_port: number },
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      // Delegate to the `klangk-hosted-url` shell script — the single source
      // of truth for hosted-URL construction, shared with setup.sh, the
      // service_command, the health check, and the shell. On success it
      // prints the URL to stdout; on error it writes a message to stderr and
      // exits non-zero, which we surface to the agent.
      let text: string;
      try {
        const { stdout } = await execFileAsync("klangk-hosted-url", [
          String(params.container_port),
        ]);
        text = stdout.trim();
      } catch (err: any) {
        text = (err.stderr || "").trim() || err.message;
      }
      return {
        content: [{ type: "text", text }],
        details: {},
      };
    },
  });
}
