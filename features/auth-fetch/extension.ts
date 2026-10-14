import { Type } from "@sinclair/typebox";

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
    name: "auth_fetch",
    description:
      "Fetch a URL that requires authentication. Opens a sign-in window " +
      "for the user when the site rejects the request, then returns the " +
      "page text once they have signed in. Use this for sites protected " +
      "by a login the user can complete in their browser.",
    parameters: Type.Object({
      url: Type.String({ description: "The URL to fetch" }),
      timeout: Type.Optional(
        Type.Number({
          description:
            "Seconds to wait for the whole interaction (default 900)",
        }),
      ),
    }),
    async execute(
      _toolCallId: string,
      params: { url: string; timeout?: number },
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
            action: "auth_fetch",
            operation: "get",
            browser_id: getBrowserId(),
            url: params.url,
            ...(params.timeout ? { timeout: params.timeout } : {}),
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

        const envelope = await resp.json();
        let result = envelope;
        if (typeof envelope.result === "string") {
          try {
            result = JSON.parse(envelope.result);
          } catch {
            // keep the envelope
          }
        }
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
              text: `Status: ${result.status}\n\n${result.body ?? ""}`,
            },
          ],
          details: {},
        };
      } catch (e: any) {
        return {
          content: [{ type: "text", text: `Auth fetch failed: ${e.message}` }],
          details: {},
        };
      }
    },
  });
}
