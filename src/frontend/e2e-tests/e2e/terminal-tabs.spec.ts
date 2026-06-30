import { test, expect } from "@playwright/test";
import WebSocket from "ws";
import { createAndOpenWorkspace, API_BASE } from "./helpers";

// Helper: open a parallel WebSocket, connect to the workspace, wait for
// container_ready, then expose send/receive for window management commands.

interface WindowInfo {
  id: string;
  index: number;
  name: string;
  active: boolean;
}

class TerminalWsClient {
  private ws: WebSocket;
  private messageQueue: Array<Record<string, unknown>> = [];
  private waiters: Array<(msg: Record<string, unknown>) => void> = [];
  private closed = false;

  constructor(ws: WebSocket) {
    this.ws = ws;
    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());
      if (this.waiters.length > 0) {
        this.waiters.shift()!(msg);
      } else {
        this.messageQueue.push(msg);
      }
    });
  }

  send(msg: Record<string, unknown>) {
    this.ws.send(JSON.stringify(msg));
  }

  async recv(timeout = 30_000): Promise<Record<string, unknown>> {
    if (this.messageQueue.length > 0) {
      return this.messageQueue.shift()!;
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error("recv timed out"));
      }, timeout);
      this.waiters.push((msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });
  }

  /** Receive messages until one matches the predicate. A transient inner recv
   *  timeout (e.g. a slow terminal backend under load) is tolerated as long as
   *  the overall deadline has not elapsed; only then does this reject. */
  async recvUntil(
    predicate: (msg: Record<string, unknown>) => boolean,
    timeout = 60_000,
  ): Promise<Record<string, unknown>> {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {
        const msg = await this.recv(deadline - Date.now());
        if (predicate(msg)) return msg;
      } catch (err) {
        // Inner recv timed out — only fail if the overall deadline passed.
        if (Date.now() >= deadline) throw err;
      }
    }
    throw new Error("recvUntil timed out");
  }

  close() {
    if (!this.closed) {
      this.closed = true;
      this.ws.close();
    }
  }
}

/** Connect to a workspace via WebSocket, wait for container_ready.
 *  Returns a client that can send window management commands. */
async function connectToWorkspace(
  token: string,
  workspaceId: string,
): Promise<TerminalWsClient> {
  const wsUrl = API_BASE.replace("http://", "ws://");

  return new Promise<TerminalWsClient>((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("connect timed out"));
    }, 60_000);

    const client = new TerminalWsClient(ws);

    ws.on("open", () => {
      client.send({ cmd: "workspace_connect", workspaceId });
    });

    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });

    // Drive through the connection flow
    (async () => {
      // Wait for container_ready
      await client.recvUntil((m) => m.type === "container_ready");
      // Send ui_ready
      client.send({ cmd: "ui_ready" });
      // Wait for container_ready
      await client.recvUntil(
        (m) =>
          m.type === "event" &&
          (m.event as Record<string, unknown>)?.name === "container_ready",
      );
      clearTimeout(timeout);
      resolve(client);
    })().catch((err) => {
      clearTimeout(timeout);
      reject(err);
    });
  });
}

/** Send a terminal_start and wait for terminal_windows response. */
async function startTerminalAndGetWindows(
  client: TerminalWsClient,
): Promise<WindowInfo[]> {
  client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
  const msg = await client.recvUntil((m) => m.type === "terminal_windows");
  return msg.windows as WindowInfo[];
}

test.describe("terminal tabs", () => {
  test("terminal starts with one window", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-init",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        const windows = await startTerminalAndGetWindows(client);
        expect(windows.length).toBe(1);
        expect(windows[0].active).toBe(true);
        expect(windows[0].index).toBe(0);
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("create a new terminal window", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-create",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create a new window
        client.send({ cmd: "terminal_new_window", name: "build" });
        const msg = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const windows = msg.windows as WindowInfo[];
        expect(windows.length).toBe(2);
        expect(windows.some((w) => w.name === "build")).toBe(true);
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("create named window rejects duplicate names", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-dup",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create first window named "test"
        client.send({ cmd: "terminal_new_window", name: "test" });
        await client.recvUntil((m) => m.type === "terminal_windows");

        // Try to create another with the same name
        client.send({ cmd: "terminal_new_window", name: "test" });
        const msg = await client.recvUntil((m) => m.type === "error");
        expect((msg.message as string).toLowerCase()).toContain(
          "already exists",
        );
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("close a terminal window", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-close",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create a second window
        client.send({ cmd: "terminal_new_window", name: "temp" });
        const created = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const tempWindow = (created.windows as WindowInfo[]).find(
          (w) => w.name === "temp",
        )!;
        expect(tempWindow).toBeDefined();

        // Close it
        client.send({
          cmd: "terminal_close_window",
          index: tempWindow.index,
        });
        const afterClose = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const remaining = afterClose.windows as WindowInfo[];
        expect(remaining.length).toBe(1);
        expect(remaining.some((w) => w.name === "temp")).toBe(false);
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("rename a terminal window", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-rename",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        const windows = await startTerminalAndGetWindows(client);
        const firstIndex = windows[0].index;

        // Rename window 0
        client.send({
          cmd: "terminal_rename_window",
          index: firstIndex,
          name: "main-shell",
        });
        const msg = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const renamed = msg.windows as WindowInfo[];
        expect(renamed[0].name).toBe("main-shell");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("rename rejects duplicate names", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-rename-dup",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create a second window named "build"
        client.send({ cmd: "terminal_new_window", name: "build" });
        await client.recvUntil((m) => m.type === "terminal_windows");

        // Try to rename window 0 to "build"
        client.send({
          cmd: "terminal_rename_window",
          index: 0,
          name: "build",
        });
        const msg = await client.recvUntil((m) => m.type === "error");
        expect((msg.message as string).toLowerCase()).toContain(
          "already exists",
        );
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("select-window switches the active window", async ({
    page,
    request,
  }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-select",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create second window
        client.send({ cmd: "terminal_new_window", name: "second" });
        const created = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const createdWindows = created.windows as WindowInfo[];
        const firstWin = createdWindows.find((w) => w.index === 0)!;
        const secondWin = createdWindows.find((w) => w.name === "second")!;

        // Switch back to window 0
        client.send({
          cmd: "terminal_select_window",
          window_id: firstWin.id,
        });

        // Verify by listing windows — window 0 should be active
        client.send({ cmd: "terminal_list_windows" });
        const listed = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const windows = listed.windows as WindowInfo[];
        const active = windows.find((w) => w.active);
        expect(active).toBeDefined();
        expect(active!.id).toBe(firstWin.id);

        // Switch to second window
        client.send({
          cmd: "terminal_select_window",
          window_id: secondWin.id,
        });
        client.send({ cmd: "terminal_list_windows" });
        const listed2 = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const windows2 = listed2.windows as WindowInfo[];
        const active2 = windows2.find((w) => w.active);
        expect(active2).toBeDefined();
        expect(active2!.id).toBe(secondWin.id);
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("list-windows returns all windows", async ({ page, request }) => {
    const { workspaceId, token, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "tabs-list",
      { waitForTerminal: true },
    );
    try {
      const client = await connectToWorkspace(token, workspaceId);
      try {
        await startTerminalAndGetWindows(client);

        // Create two more windows
        client.send({ cmd: "terminal_new_window", name: "build" });
        await client.recvUntil((m) => m.type === "terminal_windows");
        client.send({ cmd: "terminal_new_window", name: "logs" });
        await client.recvUntil((m) => m.type === "terminal_windows");

        // List all
        client.send({ cmd: "terminal_list_windows" });
        const msg = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const windows = msg.windows as WindowInfo[];
        expect(windows.length).toBe(3);
        const names = windows.map((w) => w.name);
        expect(names).toContain("build");
        expect(names).toContain("logs");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });
});
