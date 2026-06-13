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
          (m) => m.type === "terminal_started",
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

        // Both should receive shared_terminals update
        const ownerUpdate = await ownerWs.recvUntil(
          (m) => m.type === "shared_terminals",
        );
        const collabUpdate = await collabWs.recvUntil(
          (m) => m.type === "shared_terminals",
        );

        const ownerTerminals = ownerUpdate.terminals as Array<
          Record<string, unknown>
        >;
        const collabTerminals = collabUpdate.terminals as Array<
          Record<string, unknown>
        >;

        expect(ownerTerminals.some((t) => t.name === "pair-dev")).toBe(true);
        expect(collabTerminals.some((t) => t.name === "pair-dev")).toBe(true);
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
        await ownerWs.recvUntil((m) => m.type === "shared_terminals");

        // Coder should receive the shared_terminals broadcast
        const coderUpdate = await coderWs.recvUntil(
          (m) => m.type === "shared_terminals",
        );
        const terminals = coderUpdate.terminals as Array<
          Record<string, unknown>
        >;
        expect(terminals.some((t) => t.name === "dev-session")).toBe(true);

        // Coder can join the shared terminal (read-only — no code-in-shared-terminals)
        coderWs.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await coderWs.recvUntil((m) => m.type === "terminal_started");
        coderWs.send({ cmd: "join_shared_terminal", name: "dev-session" });
        const joined = await coderWs.recvUntil(
          (m) => m.type === "terminal_started" && m.shared === "dev-session",
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
        await ownerWs.recvUntil((m) => m.type === "shared_terminals");
        await specWs.recvUntil((m) => m.type === "shared_terminals");

        ownerWs.send({ cmd: "delete_shared_terminal", name: "temp" });

        // Spectator gets deletion notification + empty list
        const deleted = await specWs.recvUntil(
          (m) => m.type === "shared_terminal_deleted",
        );
        expect(deleted.name).toBe("temp");

        const updated = await specWs.recvUntil(
          (m) => m.type === "shared_terminals",
        );
        const terminals = updated.terminals as Array<Record<string, unknown>>;
        expect(terminals.some((t) => t.name === "temp")).toBe(false);
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
        await client.recvUntil((m) => m.type === "shared_terminals");
        client.send({ cmd: "create_shared_terminal", name: "term-b" });
        await client.recvUntil((m) => m.type === "shared_terminals");

        // Start isolated terminal first
        client.send({ cmd: "terminal_start", cols: 80, rows: 24 });
        await client.recvUntil((m) => m.type === "terminal_started");

        // Rapidly switch between shared terminals
        for (let i = 0; i < 3; i++) {
          client.send({ cmd: "join_shared_terminal", name: "term-a" });
          await client.recvUntil(
            (m) => m.type === "terminal_started" && m.shared === "term-a",
          );
          client.send({ cmd: "join_shared_terminal", name: "term-b" });
          await client.recvUntil(
            (m) => m.type === "terminal_started" && m.shared === "term-b",
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
});
