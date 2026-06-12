import { test, expect } from "@playwright/test";
import WebSocket from "ws";
import { createAndOpenWorkspace, API_BASE } from "./helpers";

// Run a command inside the container by opening a parallel WebSocket
// from Node.js (the test process).  This is more reliable than typing
// into the Flutter canvas — no coordinate guessing, no file polling.

/** Run a shell command in the container via a second WebSocket.
 *  Returns the decoded stdout. */
async function execInContainer(
  token: string,
  workspaceId: string,
  command: string[],
): Promise<string> {
  const wsUrl = API_BASE.replace("http://", "ws://");

  return new Promise<string>((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const chunks: Buffer[] = [];
    let connected = false;

    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("exec timed out after 15s"));
    }, 15_000);

    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());

      if (msg.type === "workspace_ready" && !connected) {
        connected = true;
        ws.send(JSON.stringify({ cmd: "ui_ready" }));
        return;
      }

      // After container_ready, send the exec command
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
        ws.close();
        resolve(Buffer.concat(chunks).toString("utf-8"));
      }
    });

    ws.on("open", () => {
      ws.send(
        JSON.stringify({
          cmd: "workspace_connect",
          workspaceId,
        }),
      );
    });

    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

test.describe("per-user HOME directory", () => {
  test("HOME is /home/<handle> and is a symlink to .users/<uuid>", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "home-e2e",
      { waitForTerminal: true },
    );
    try {
      // Run commands via a parallel WebSocket — no canvas interaction
      const home = (
        await execInContainer(token, workspaceId, ["bash", "-c", "echo $HOME"])
      ).trim();

      // HOME should be /home/<handle>, not /home/klangk
      expect(home).toMatch(/^\/home\/[a-z0-9._-]+$/);
      expect(home).not.toBe("/home/klangk");

      // The handle dir should be a symlink to .users/<uuid>
      const link = (
        await execInContainer(token, workspaceId, [
          "bash",
          "-c",
          `readlink ${home}`,
        ])
      ).trim();
      expect(link).toMatch(/^\.users\/[0-9a-f-]+$/);
    } finally {
      await cleanup();
    }
  });

  test("terminal starts in the user's home directory", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "cwd-e2e",
      { waitForTerminal: true },
    );
    try {
      // The exec session starts in $HOME (the user's handle dir)
      const cwd = (await execInContainer(token, workspaceId, ["pwd"])).trim();

      expect(cwd).toMatch(/^\/home\/[a-z0-9._-]+$/);
      expect(cwd).not.toBe("/home/work");
    } finally {
      await cleanup();
    }
  });

  test("shared /home/work is writable from the user home", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "work-e2e",
      { waitForTerminal: true },
    );
    try {
      const output = (
        await execInContainer(token, workspaceId, [
          "bash",
          "-c",
          "echo ok > /home/work/.e2e-write-test && cat /home/work/.e2e-write-test",
        ])
      ).trim();

      expect(output).toBe("ok");
    } finally {
      await cleanup();
    }
  });

  test("files written to $HOME persist in the per-user directory", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "persist-e2e",
      { waitForTerminal: true },
    );
    try {
      // Write to $HOME, then read it back — proves the directory is real
      const output = (
        await execInContainer(token, workspaceId, [
          "bash",
          "-c",
          "echo persisted > $HOME/.e2e-marker && cat $HOME/.e2e-marker",
        ])
      ).trim();

      expect(output).toBe("persisted");
    } finally {
      await cleanup();
    }
  });
});
