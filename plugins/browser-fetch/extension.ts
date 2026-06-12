import { Type } from "@sinclair/typebox";

const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;
const BRIDGE_TOKEN = process.env.KLANGK_BRIDGE_TOKEN;
const WORKSPACE_TOKEN = process.env.KLANGK_WORKSPACE_TOKEN;

export default function (pi: any) {
  if (!BRIDGE_URL || !BRIDGE_TOKEN) return;

  pi.registerTool({
    name: "browser_fetch",
    description:
      "Fetch a URL using the user's browser session credentials. " +
      "Use this when you need to access a URL that requires the " +
      "user's authentication (cookies, OAuth tokens, etc.).",
    parameters: Type.Object({
      url: Type.String({ description: "The URL to fetch" }),
      method: Type.Optional(
        Type.String({
          description: "HTTP method (default: GET)",
          enum: ["GET", "POST", "PUT", "DELETE", "PATCH"],
        }),
      ),
      headers: Type.Optional(
        Type.Record(Type.String(), Type.String(), {
          description: "Request headers",
        }),
      ),
      body: Type.Optional(
        Type.String({ description: "Request body (for POST/PUT/PATCH)" }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: {
        url: string;
        method?: string;
        headers?: Record<string, string>;
        body?: string;
      },
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
            action: "fetch",
            token: BRIDGE_TOKEN,
            url: params.url,
            method: params.method || "GET",
            headers: params.headers || {},
            body: params.body || null,
          }),
        });

        if (!resp.ok) {
          const text = await resp.text();
          return {
            content: [
              { type: "text", text: `Bridge error (${resp.status}): ${text}` },
            ],
            details: {},
          };
        }

        const result = await resp.json();
        if (result.error) {
          return {
            content: [{ type: "text", text: `Error: ${result.error}` }],
            details: {},
          };
        }

        return {
          content: [
            {
              type: "text",
              text: `Status: ${result.status}\n\n${result.body}`,
            },
          ],
          details: {},
        };
      } catch (e: any) {
        return {
          content: [{ type: "text", text: `Fetch failed: ${e.message}` }],
          details: {},
        };
      }
    },
  });
}
