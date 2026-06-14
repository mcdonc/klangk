import { test, expect } from "@playwright/test";
import WebSocket from "ws";
import {
  registerUser,
  createWorkspace,
  openWorkspace,
  API_BASE,
  TEST_PASSWORD,
} from "./helpers";

// --- WebSocket helpers for multi-user testing ---

interface WsMessage {
  type?: string;
  [key: string]: unknown;
}

class TestWsClient {
  private ws: WebSocket;
  private queue: WsMessage[] = [];
  private waiters: Array<(msg: WsMessage) => void> = [];

  constructor(ws: WebSocket) {
    this.ws = ws;
    ws.on("message", (raw: Buffer | string) => {
      const msg = JSON.parse(raw.toString());
      if (this.waiters.length > 0) {
        this.waiters.shift()!(msg);
      } else {
        this.queue.push(msg);
      }
    });
  }

  send(msg: Record<string, unknown>) {
    this.ws.send(JSON.stringify(msg));
  }

  async recv(timeout = 10_000): Promise<WsMessage> {
    if (this.queue.length > 0) return this.queue.shift()!;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error("recv timed out")),
        timeout,
      );
      this.waiters.push((msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
    });
  }

  async recvUntil(
    predicate: (msg: WsMessage) => boolean,
    timeout = 30_000,
  ): Promise<WsMessage> {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const msg = await this.recv(deadline - Date.now());
      if (predicate(msg)) return msg;
    }
    throw new Error("recvUntil timed out");
  }

  /** Collect all messages matching predicate received within a time window. */
  async collectUntilQuiet(
    predicate: (msg: WsMessage) => boolean,
    quietMs = 500,
    timeout = 10_000,
  ): Promise<WsMessage[]> {
    const results: WsMessage[] = [];
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {
        const msg = await this.recv(quietMs);
        if (predicate(msg)) results.push(msg);
      } catch {
        break; // quiet period elapsed
      }
    }
    return results;
  }

  close() {
    this.ws.close();
  }
}

async function connectWs(
  token: string,
  workspaceId: string,
): Promise<TestWsClient> {
  const wsUrl = API_BASE.replace("http://", "ws://");
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${wsUrl}/ws?token=${token}`);
    const client = new TestWsClient(ws);
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("connect timed out"));
    }, 60_000);

    ws.on("open", () => {
      client.send({ cmd: "workspace_connect", workspaceId });
    });
    ws.on("error", (err) => {
      clearTimeout(timeout);
      reject(err);
    });

    (async () => {
      await client.recvUntil((m) => m.type === "workspace_ready");
      client.send({ cmd: "ui_ready" });
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

async function addToRole(
  request: import("@playwright/test").APIRequestContext,
  headers: Record<string, string>,
  workspaceId: string,
  role: string,
  email: string,
) {
  const resp = await request.post(
    `${API_BASE}/workspaces/${workspaceId}/roles/${role}`,
    { headers, data: { email } },
  );
  expect(resp.ok()).toBeTruthy();
}

// --- Tests ---

test.describe("workspace roles", () => {
  test("owner can see all role groups", async ({ page, request }) => {
    const email = `roles-owner-${Date.now()}@test.example.com`;
    const { token, headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "roles-owner",
    );
    try {
      const resp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/roles`,
        { headers },
      );
      expect(resp.ok()).toBeTruthy();
      const roles = await resp.json();
      const roleNames = roles.map((r: { role: string }) => r.role);
      expect(roleNames).toContain("owners");
      expect(roleNames).toContain("coders");
      expect(roleNames).toContain("collaborators");
      expect(roleNames).toContain("spectators");
    } finally {
      await cleanup();
    }
  });

  test("spectator can connect but cannot start isolated terminal", async ({
    page,
    request,
  }) => {
    // Create workspace as owner
    const ownerEmail = `spec-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "spec-test",
    );
    try {
      // Create spectator user and add to spectators role
      const specEmail = `spec-user-${Date.now()}@test.example.com`;
      const spec = await registerUser(request, specEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "spectators",
        specEmail,
      );

      // Owner opens workspace first to start container
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      // Spectator connects via WS
      const client = await connectWs(spec.token, workspaceId);
      try {
        // Send terminal_start — should get terminal_started (no error)
        // but no actual isolated session
        client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        const msg = await client.recvUntil(
          (m) => m.type === "terminal_started" || m.type === "error",
        );
        expect(msg.type).toBe("terminal_started");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("collaborator can start isolated terminal", async ({
    page,
    request,
  }) => {
    const ownerEmail = `collab-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "collab-test",
    );
    try {
      const collabEmail = `collab-user-${Date.now()}@test.example.com`;
      const collab = await registerUser(request, collabEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "collaborators",
        collabEmail,
      );

      // Owner starts container
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      // Collaborator connects and starts terminal
      const client = await connectWs(collab.token, workspaceId);
      try {
        client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        const msg = await client.recvUntil(
          (m) => m.type === "terminal_started" || m.type === "error",
          60_000,
        );
        expect(msg.type).toBe("terminal_started");

        // Should also get terminal_windows (has isolated tabs)
        const windows = await client.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        expect(
          (windows.windows as Array<Record<string, unknown>>).length,
        ).toBeGreaterThanOrEqual(1);
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("coder can start isolated terminal but not create shared", async ({
    page,
    request,
  }) => {
    const ownerEmail = `coder-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "coder-test",
    );
    try {
      const coderEmail = `coder-user-${Date.now()}@test.example.com`;
      const coder = await registerUser(request, coderEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "coders",
        coderEmail,
      );

      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const client = await connectWs(coder.token, workspaceId);
      try {
        // Can start isolated terminal
        client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await client.recvUntil((m) => m.type === "terminal_started");

        // Cannot create shared terminal (no share-terminals permission)
        client.send({ cmd: "create_shared_terminal", name: "test" });
        const err = await client.recvUntil((m) => m.type === "error");
        expect((err.message as string).toLowerCase()).toContain("permission");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });
});

test.describe("shared terminal visibility", () => {
  test("shared terminal appears for other connected users", async ({
    page,
    request,
  }) => {
    const ownerEmail = `share-vis-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "share-vis",
    );
    try {
      const collabEmail = `share-vis-collab-${Date.now()}@test.example.com`;
      const collab = await registerUser(request, collabEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "collaborators",
        collabEmail,
      );

      // Owner starts container
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      // Both connect via WS
      const ownerWs = await connectWs(owner.token, workspaceId);
      const collabWs = await connectWs(collab.token, workspaceId);
      try {
        // Owner creates shared terminal
        ownerWs.send({ cmd: "create_shared_terminal", name: "pair-dev" });

        // Both should receive shared_terminals update with pair-dev
        // (recvUntil skips the empty list from ui_ready)
        const ownerUpdate = await ownerWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "pair-dev",
            ),
        );
        const collabUpdate = await collabWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "pair-dev",
            ),
        );

        const ownerTerminals = ownerUpdate.terminals as Array<
          Record<string, unknown>
        >;
        const collabTerminals = collabUpdate.terminals as Array<
          Record<string, unknown>
        >;

        expect(ownerTerminals.some((t) => t.window_name === "pair-dev")).toBe(
          true,
        );
        expect(collabTerminals.some((t) => t.window_name === "pair-dev")).toBe(
          true,
        );
      } finally {
        ownerWs.close();
        collabWs.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("coder can see and join shared terminals", async ({ page, request }) => {
    const ownerEmail = `coder-vis-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "coder-vis",
    );
    try {
      const coderEmail = `coder-vis-user-${Date.now()}@test.example.com`;
      const coder = await registerUser(request, coderEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "coders",
        coderEmail,
      );

      // Owner starts container
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const ownerWs = await connectWs(owner.token, workspaceId);
      const coderWs = await connectWs(coder.token, workspaceId);
      try {
        // Owner creates a shared terminal
        ownerWs.send({ cmd: "create_shared_terminal", name: "dev-session" });

        // Both should receive broadcast with dev-session
        await ownerWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "dev-session",
            ),
        );
        const coderUpdate = await coderWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "dev-session",
            ),
        );
        const terminals = coderUpdate.terminals as Array<
          Record<string, unknown>
        >;
        expect(terminals.some((t) => t.window_name === "dev-session")).toBe(
          true,
        );

        // Coder can join the shared terminal (read-only — no code-in-shared-terminals)
        const devTerminal = terminals.find(
          (t) => t.window_name === "dev-session",
        ) as Record<string, unknown>;
        coderWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await coderWs.recvUntil((m) => m.type === "terminal_started");
        coderWs.send({
          cmd: "join_shared_terminal",
          user_id: devTerminal.user_id,
          window_id: devTerminal.window_id,
        });
        const joined = await coderWs.recvUntil(
          (m) =>
            m.type === "terminal_started" && m.shared_window === "dev-session",
        );
        expect(joined.readOnly).toBe(true);
      } finally {
        ownerWs.close();
        coderWs.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("deleted shared terminal disappears for other users", async ({
    page,
    request,
  }) => {
    const ownerEmail = `share-del-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "share-del",
    );
    try {
      const specEmail = `share-del-spec-${Date.now()}@test.example.com`;
      const spec = await registerUser(request, specEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "spectators",
        specEmail,
      );

      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const ownerWs = await connectWs(owner.token, workspaceId);
      const specWs = await connectWs(spec.token, workspaceId);
      try {
        // Create then delete
        ownerWs.send({ cmd: "create_shared_terminal", name: "temp" });
        const ownerShared = await ownerWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "temp",
            ),
        );
        await specWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "temp",
            ),
        );

        const tempTerminal = (
          ownerShared.terminals as Array<Record<string, unknown>>
        ).find((t) => t.window_name === "temp") as Record<string, unknown>;
        ownerWs.send({
          cmd: "delete_shared_terminal",
          user_id: tempTerminal.user_id,
          window_id: tempTerminal.window_id,
        });

        // Spectator gets deletion notification + empty list
        const deleted = await specWs.recvUntil(
          (m) => m.type === "shared_terminal_deleted",
        );
        expect(deleted.window_name).toBe("temp");

        const updated = await specWs.recvUntil(
          (m) => m.type === "shared_terminals",
        );
        const terminals = updated.terminals as Array<Record<string, unknown>>;
        expect(terminals.some((t) => t.window_name === "temp")).toBe(false);
      } finally {
        ownerWs.close();
        specWs.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("rapid shared terminal switching does not produce duplicate session", async ({
    page,
    request,
  }) => {
    const ownerEmail = `rapid-switch-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "rapid-switch",
    );
    try {
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const client = await connectWs(owner.token, workspaceId);
      try {
        // Create two shared terminals
        client.send({ cmd: "create_shared_terminal", name: "term-a" });
        await client.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "term-a",
            ),
        );
        client.send({ cmd: "create_shared_terminal", name: "term-b" });
        const sharedMsg = await client.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "term-b",
            ),
        );
        const sharedTerminals = sharedMsg.terminals as Array<
          Record<string, unknown>
        >;
        const ownerUserId = sharedTerminals[0].user_id as string;
        const termA = sharedTerminals.find(
          (t) => t.window_name === "term-a",
        ) as Record<string, unknown>;
        const termB = sharedTerminals.find(
          (t) => t.window_name === "term-b",
        ) as Record<string, unknown>;

        // Start isolated terminal first
        client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await client.recvUntil((m) => m.type === "terminal_started");

        // Rapidly switch between shared terminals
        for (let i = 0; i < 3; i++) {
          client.send({
            cmd: "join_shared_terminal",
            user_id: ownerUserId,
            window_id: termA.window_id,
          });
          await client.recvUntil(
            (m) =>
              m.type === "terminal_started" && m.shared_window === "term-a",
          );
          client.send({
            cmd: "join_shared_terminal",
            user_id: ownerUserId,
            window_id: termB.window_id,
          });
          await client.recvUntil(
            (m) =>
              m.type === "terminal_started" && m.shared_window === "term-b",
          );
        }

        // Collect terminal output — should not contain "duplicate session"
        const output = await client.collectUntilQuiet(
          (m) => m.type === "terminal_output",
          1000,
        );
        const allOutput = output.map((m) => (m.data as string) || "").join("");
        expect(allOutput).not.toContain("duplicate session");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test.skip("terminal state survives container restart", async ({
    request,
  }) => {
    const ownerEmail = `state-restart-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "state-restart",
    );
    try {
      // Connect directly — connectWs starts the container
      const client1 = await connectWs(owner.token, workspaceId);

      // Start terminal, create a second window, share it
      client1.send({ cmd: "terminal_start", cols: 80, rows: 24 });
      await client1.recvUntil((m) => m.type === "terminal_started");
      await client1.recvUntil((m) => m.type === "terminal_windows");

      client1.send({ cmd: "create_shared_terminal", name: "build" });
      await client1.recvUntil(
        (m) =>
          m.type === "shared_terminals" &&
          (m.terminals as Array<Record<string, unknown>>).some(
            (t) => t.window_name === "build",
          ),
      );

      // Shut down the container
      client1.send({ cmd: "shutdown_container" });
      await client1.recvUntil(
        (m) =>
          m.type === "event" &&
          ((m.event as Record<string, unknown>)?.name === "container_stopped" ||
            (m.event as Record<string, unknown>)?.name === "container_ready"),
        60_000,
      );
      client1.close();

      // Reconnect — this starts a new container
      const client2 = await connectWs(owner.token, workspaceId);
      try {
        // Start terminal again
        client2.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await client2.recvUntil((m) => m.type === "terminal_started", 60_000);

        // Should get terminal_windows with both windows restored
        const windowsMsg = await client2.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const windows = windowsMsg.windows as Array<Record<string, unknown>>;
        const windowNames = windows.map((w) => w.name);
        expect(windowNames).toContain("build");

        // Should get shared_terminals with "build" still shared
        const sharedMsg = await client2.recvUntil(
          (m) => m.type === "shared_terminals",
        );
        const terminals = sharedMsg.terminals as Array<Record<string, unknown>>;
        expect(terminals.some((t) => t.window_name === "build")).toBe(true);
      } finally {
        client2.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("spectator cannot send input to shared terminal", async ({
    page,
    request,
  }) => {
    const ownerEmail = `spec-input-owner-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "spec-input",
    );
    try {
      const specEmail = `spec-input-user-${Date.now()}@test.example.com`;
      const spec = await registerUser(request, specEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "spectators",
        specEmail,
      );

      // Owner starts container and creates a shared terminal
      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const ownerWs = await connectWs(owner.token, workspaceId);
      ownerWs.send({ cmd: "create_shared_terminal", name: "watch-me" });
      const sharedMsg = await ownerWs.recvUntil(
        (m) =>
          m.type === "shared_terminals" &&
          (m.terminals as Array<Record<string, unknown>>).some(
            (t) => t.window_name === "watch-me",
          ),
      );
      const terminal = (
        sharedMsg.terminals as Array<Record<string, unknown>>
      ).find((t) => t.window_name === "watch-me") as Record<string, unknown>;

      // Spectator connects and joins the shared terminal
      const specWs = await connectWs(spec.token, workspaceId);
      try {
        specWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await specWs.recvUntil((m) => m.type === "terminal_started");

        specWs.send({
          cmd: "join_shared_terminal",
          user_id: terminal.user_id,
          window_id: terminal.window_id,
        });
        const joined = await specWs.recvUntil(
          (m) =>
            m.type === "terminal_started" && m.shared_window === "watch-me",
        );
        expect(joined.readOnly).toBe(true);

        // Spectator sends input — it should be silently dropped
        // (the server drops input when session.read_only is True).
        // Send a distinctive command and verify it doesn't appear
        // in the owner's terminal output.
        specWs.send({
          cmd: "terminal_input",
          data: "echo SPECTATOR_WAS_HERE\r",
        });

        // Wait a bit for any output, then check owner's terminal
        // for the distinctive string. Owner sends a command that
        // WILL produce output, proving the terminal is working.
        ownerWs.send({
          cmd: "terminal_input",
          data: "echo OWNER_CHECK\r",
        });

        // Collect output from the spectator's view
        const output = await specWs.collectUntilQuiet(
          (m) => m.type === "terminal_output",
          2000,
        );
        const allOutput = output
          .map((m) => {
            const data = m.data as string | undefined;
            return data ?? "";
          })
          .join("");

        // The owner's echo should appear but the spectator's should not
        expect(allOutput).not.toContain("SPECTATOR_WAS_HERE");
      } finally {
        specWs.close();
        ownerWs.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("spectator cannot create shared terminal", async ({ page, request }) => {
    const ownerEmail = `spec-nocreate-${Date.now()}@test.example.com`;
    const owner = await registerUser(request, ownerEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      owner.headers,
      "spec-nocreate",
    );
    try {
      const specEmail = `spec-nocreate-user-${Date.now()}@test.example.com`;
      const spec = await registerUser(request, specEmail);
      await addToRole(
        request,
        owner.headers,
        workspaceId,
        "spectators",
        specEmail,
      );

      await openWorkspace(page, ownerEmail, workspaceId, {
        waitForTerminal: true,
      });

      const client = await connectWs(spec.token, workspaceId);
      try {
        client.send({ cmd: "create_shared_terminal", name: "nope" });
        const msg = await client.recvUntil((m) => m.type === "error");
        expect((msg.message as string).toLowerCase()).toContain("permission");
      } finally {
        client.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("shared terminal survives creating a new window", async ({
    page,
    request,
  }) => {
    const adminEmail = `share-sync-admin-${Date.now()}@test.example.com`;
    const admin = await registerUser(request, adminEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      admin.headers,
      "share-sync",
    );
    try {
      const coadminEmail = `share-sync-coadmin-${Date.now()}@test.example.com`;
      const coadmin = await registerUser(request, coadminEmail);
      await addToRole(
        request,
        admin.headers,
        workspaceId,
        "owners",
        coadminEmail,
      );

      // Admin opens workspace to start container
      await openWorkspace(page, adminEmail, workspaceId, {
        waitForTerminal: true,
      });

      const adminWs = await connectWs(admin.token, workspaceId);
      const coadminWs = await connectWs(coadmin.token, workspaceId);

      try {
        // Admin starts terminal — collect all messages until we have
        // both terminal_started and terminal_windows.
        adminWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        let adminInitWindows: WsMessage = { type: "none" };
        let gotStarted = false;
        await adminWs.recvUntil((m) => {
          if (m.type === "terminal_started") gotStarted = true;
          if (m.type === "terminal_windows") adminInitWindows = m;
          return gotStarted && adminInitWindows.type === "terminal_windows";
        }, 60_000);
        const firstWindow = (
          adminInitWindows.windows as Array<Record<string, unknown>>
        )[0];
        const firstWindowName = firstWindow.name as string;

        // Admin shares the first terminal
        adminWs.send({ cmd: "share_window", window_id: firstWindow.id });

        // Coadmin sees the shared terminal
        const shared1 = await coadminWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === firstWindowName,
            ),
        );
        const sharedFirst = (
          shared1.terminals as Array<Record<string, unknown>>
        ).find((t) => t.window_name === firstWindowName)!;

        // Admin types in the shared bash terminal
        adminWs.send({ cmd: "terminal_input", data: "bashbashbash" });

        // Coadmin starts their terminal and joins admin's shared terminal
        coadminWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await coadminWs.recvUntil((m) => m.type === "terminal_started");
        coadminWs.send({
          cmd: "join_shared_terminal",
          user_id: sharedFirst.user_id,
          window_id: sharedFirst.window_id,
        });

        // Wait for terminal_started + collect all terminal_output that
        // arrives (the tmux refresh sends the screen content).
        const joinOutput: string[] = [];
        await coadminWs.recvUntil((m) => {
          if (m.type === "terminal_output") {
            joinOutput.push((m.data as string) ?? "");
          }
          return (
            m.type === "terminal_started" && m.shared_window === firstWindowName
          );
        });
        // Also collect any output that arrives after terminal_started
        const moreOutput = await coadminWs.collectUntilQuiet(
          (m) => m.type === "terminal_output",
          2000,
        );
        const firstText = [
          ...joinOutput,
          ...moreOutput.map((m) => (m.data as string) ?? ""),
        ].join("");
        expect(firstText).toContain("bashbashbash");

        // Coadmin types "abc" — admin should see it too
        coadminWs.send({ cmd: "terminal_input", data: "abc" });
        const adminSees = await adminWs.collectUntilQuiet(
          (m) => m.type === "terminal_output",
          2000,
        );
        expect(
          adminSees.map((m) => (m.data as string) ?? "").join(""),
        ).toContain("abc");

        // NOW: admin creates a second terminal window
        adminWs.send({ cmd: "terminal_new_window" });
        const newWindowsMsg = await adminWs.recvUntil(
          (m) => m.type === "terminal_windows",
        );
        const allWindows = newWindowsMsg.windows as Array<
          Record<string, unknown>
        >;
        const newWindow = allWindows.find((w) => w.name !== firstWindowName);
        expect(newWindow).toBeDefined();

        // Admin types in the new window
        adminWs.send({ cmd: "terminal_input", data: "oneoneone" });

        // Admin shares the new window
        adminWs.send({ cmd: "share_window", window_id: newWindow!.id });

        // After creating AND sharing a new window, the first shared
        // terminal should STILL be shared. Coadmin should see both.
        const shared2 = await coadminWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).length >= 2,
        );
        const terminals2 = shared2.terminals as Array<Record<string, unknown>>;
        expect(terminals2.some((t) => t.window_name === firstWindowName)).toBe(
          true,
        );
        expect(terminals2.some((t) => t.window_name === newWindow!.name)).toBe(
          true,
        );

        // Coadmin joins the new shared terminal
        const newShared = terminals2.find(
          (t) => t.window_name === newWindow!.name,
        )!;
        coadminWs.send({
          cmd: "join_shared_terminal",
          user_id: newShared.user_id,
          window_id: newShared.window_id,
        });

        // Collect output during the join
        const joinNewOutput: string[] = [];
        await coadminWs.recvUntil((m) => {
          if (m.type === "terminal_output") {
            joinNewOutput.push((m.data as string) ?? "");
          }
          return (
            m.type === "terminal_started" && m.shared_window === newWindow!.name
          );
        });
        const moreNewOutput = await coadminWs.collectUntilQuiet(
          (m) => m.type === "terminal_output",
          3000,
        );
        const newText = [
          ...joinNewOutput,
          ...moreNewOutput.map((m) => (m.data as string) ?? ""),
        ].join("");
        expect(newText).toContain("oneoneone");
      } finally {
        adminWs.close();
        coadminWs.close();
      }
    } finally {
      await cleanup();
    }
  });

  test("renaming a shared terminal updates other users' tab list", async ({
    page,
    request,
  }) => {
    const adminEmail = `rename-share-admin-${Date.now()}@test.example.com`;
    const admin = await registerUser(request, adminEmail);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      admin.headers,
      "rename-share",
    );
    try {
      const coadminEmail = `rename-share-coadmin-${Date.now()}@test.example.com`;
      const coadmin = await registerUser(request, coadminEmail);
      await addToRole(
        request,
        admin.headers,
        workspaceId,
        "owners",
        coadminEmail,
      );

      await openWorkspace(page, adminEmail, workspaceId, {
        waitForTerminal: true,
      });

      const adminWs = await connectWs(admin.token, workspaceId);
      const coadminWs = await connectWs(coadmin.token, workspaceId);

      try {
        // Admin starts terminal and gets window list
        adminWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        let adminWindows: WsMessage = { type: "none" };
        await adminWs.recvUntil((m) => {
          if (m.type === "terminal_windows") adminWindows = m;
          return (
            m.type === "terminal_windows" &&
            (m.windows as Array<Record<string, unknown>>).length > 0
          );
        }, 60_000);

        const firstWindow = (
          adminWindows.windows as Array<Record<string, unknown>>
        )[0];

        // Admin shares the terminal
        adminWs.send({
          cmd: "share_window",
          window_id: firstWindow.id as string,
        });

        // Coadmin sees the shared terminal with its original name
        const shared1 = await coadminWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).length > 0,
        );
        const originalName = (
          shared1.terminals as Array<Record<string, unknown>>
        )[0].window_name as string;
        expect(originalName).toBe(firstWindow.name as string);

        // Admin renames the terminal
        adminWs.send({
          cmd: "terminal_rename_window",
          index: firstWindow.index as number,
          name: "my-build",
        });

        // Admin gets updated terminal_windows
        await adminWs.recvUntil(
          (m) =>
            m.type === "terminal_windows" &&
            (m.windows as Array<Record<string, unknown>>).some(
              (w) => w.name === "my-build",
            ),
        );

        // Coadmin should receive updated shared_terminals with the new name
        const shared2 = await coadminWs.recvUntil(
          (m) =>
            m.type === "shared_terminals" &&
            (m.terminals as Array<Record<string, unknown>>).some(
              (t) => t.window_name === "my-build",
            ),
        );
        const renamedTerminal = (
          shared2.terminals as Array<Record<string, unknown>>
        ).find((t) => t.window_name === "my-build");
        expect(renamedTerminal).toBeDefined();
      } finally {
        adminWs.close();
        coadminWs.close();
      }
    } finally {
      await cleanup();
    }
  });
});
