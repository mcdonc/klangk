import { test, expect } from "@playwright/test";
import WebSocket from "ws";
import { createAndOpenWorkspace, API_BASE } from "./helpers";

/** Run a shell command in the container via a WebSocket exec session. */
async function execInContainer(
  token: string,
  workspaceId: string,
  command: string[],
): Promise<{ stdout: string; exitCode: number }> {
  const wsUrl = API_BASE.replace("http://", "ws://");

  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const chunks: Buffer[] = [];
    let connected = false;
    let exitCode = 0;

    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("exec timed out after 15s"));
    }, 15_000);

    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());

      if (msg.type === "container_ready" && !connected) {
        connected = true;
        ws.send(JSON.stringify({ cmd: "ui_ready" }));
        return;
      }

      if (
        msg.type === "event" &&
        msg.event?.name === "container_ready" &&
        connected
      ) {
        ws.send(JSON.stringify({ cmd: "exec_start", command }));
        return;
      }

      if (msg.type === "exec_output") {
        chunks.push(Buffer.from(msg.data, "base64"));
      }

      if (msg.type === "exec_exit") {
        clearTimeout(timeout);
        exitCode = msg.exit_code ?? 0;
        ws.close();
        resolve({
          stdout: Buffer.concat(chunks).toString("utf-8"),
          exitCode,
        });
      }
    });

    ws.on("open", () => {
      ws.send(JSON.stringify({ cmd: "workspace_connect", workspaceId }));
    });

    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

// The E2E server starts without KLANGKD_ALLOW_SUDO, so the deploy
// ceiling is on — but the bag value is the sole posture source
// (#3047): an absent allow_sudo key means sudo is OFF; sudo needs an
// explicit opt-in.
test.describe("sudo configuration", () => {
  test("sudo is off by default (absent key locks down)", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "sudo-default-e2e",
      { waitForTerminal: true },
    );
    try {
      // sudo -n = non-interactive; with no allow_sudo key the sudoers
      // rule is !ALL, so it must fail.
      const result = await execInContainer(token, workspaceId, [
        "bash",
        "-c",
        "sudo -n true 2>&1; echo EXIT:$?",
      ]);
      expect(result.stdout).toContain("EXIT:1");
    } finally {
      await cleanup();
    }
  });

  test("an explicit allow_sudo=true opts in", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "sudo-on-e2e",
      {
        waitForTerminal: true,
        body: { settings: { allow_sudo: true } },
      },
    );
    try {
      // sudo -n = non-interactive; should succeed (exit 0) with the
      // NOPASSWD sudoers rule.
      const result = await execInContainer(token, workspaceId, [
        "bash",
        "-c",
        "sudo -n true 2>&1; echo EXIT:$?",
      ]);
      expect(result.stdout).toContain("EXIT:0");
    } finally {
      await cleanup();
    }
  });

  test("per-workspace lock-down disables sudo below the deploy default", async ({
    page,
    request,
  }) => {
    // #2017: settings.allow_sudo=false opts this workspace out even on
    // the (now sudo-enabled) deploy.
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "sudo-locked-e2e",
      {
        waitForTerminal: true,
        body: { settings: { allow_sudo: false } },
      },
    );
    try {
      const result = await execInContainer(token, workspaceId, [
        "bash",
        "-c",
        "sudo -n true 2>&1; echo EXIT:$?",
      ]);
      expect(result.stdout).toContain("EXIT:1");
    } finally {
      await cleanup();
    }
  });
});
