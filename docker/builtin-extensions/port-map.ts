import { Type } from "@sinclair/typebox";

export default function (pi: any) {
  // Parse BARK_PORT_MAPPINGS: "8000:9000,8001:9001,8002:9002,..."
  const portMap: Map<number, number> = new Map();
  const mappingsStr = process.env.BARK_PORT_MAPPINGS || "";
  for (const pair of mappingsStr.split(",")) {
    const [container, host] = pair.split(":").map(Number);
    if (!isNaN(container) && !isNaN(host)) {
      portMap.set(container, host);
    }
  }

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
      const port = params.container_port;
      const externalPort = portMap.get(port);
      if (externalPort === undefined) {
        const mapped = Array.from(portMap.keys()).sort((a, b) => a - b);
        return {
          content: [
            {
              type: "text",
              text: `Port ${port} is not a mapped port. Mapped container ports: ${mapped.join(", ")}. Use one of these ports for your server.`,
            },
          ],
          details: {},
        };
      }
      const proto = process.env.BARK_HOSTING_PROTO || "http";
      const hostname = process.env.BARK_HOSTING_HOSTNAME || "localhost";
      const basePath = process.env.BARK_HOSTING_BASE_PATH || "";
      const workspaceId = process.env.BARK_WORKSPACE_ID || "";
      const url = `${proto}://${hostname}${basePath}/hosted/${workspaceId}/${externalPort}/`;
      return {
        content: [
          {
            type: "text",
            text: `Container port ${port} is mapped to external port ${externalPort}. The user can access it at ${url}`,
          },
        ],
        details: {},
      };
    },
  });
}
