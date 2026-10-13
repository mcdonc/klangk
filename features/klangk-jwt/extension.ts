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
    name: "klangk_jwt",
    description:
      "Retrieves the user's klangk web session JWT (the klangk_jwt " +
      "sessionStorage value) from the browser via the bridge. The browser " +
      "asks the user to confirm each retrieval unless the deploy disabled " +
      "the prompt. Only use this when the user explicitly asks for the " +
      "token; never print it into logs or files unless told to.",
    parameters: Type.Object({}),
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      _signal: AbortSignal | undefined,
      _onUpdate: any,
      _ctx: any,
    ) {
      const browserId = getBrowserId();
      if (!browserId) {
        return {
          content: [{ type: "text", text: "No browser connected." }],
          details: {},
        };
      }

      const token = getWorkspaceToken();
      try {
        const resp = await fetch(`${BRIDGE_URL}/api/v1/browser-delegate`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            action: "klangk_jwt",
            browser_id: browserId,
          }),
        });

        const data = (await resp.json()) as {
          token?: string;
          error?: string;
          detail?: string;
        };
        if (resp.ok && data.token) {
          return {
            content: [{ type: "text", text: data.token }],
            details: {},
          };
        }
        const reason = data.error || data.detail || `HTTP ${resp.status}`;
        return {
          content: [{ type: "text", text: `klangk_jwt failed: ${reason}` }],
          details: {},
        };
      } catch (e) {
        return {
          content: [{ type: "text", text: `klangk_jwt failed: ${e}` }],
          details: {},
        };
      }
    },
  });
}
