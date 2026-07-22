import { Type } from "@sinclair/typebox";
import { execSync } from "child_process";

// Pi extension: exposes Soliplex room tools to the agent. Each tool delegates
// to the user's browser session (where the Flutter soliplex feature holds the
// auth + talks to the Soliplex server) via the klangk browser-delegate bridge.
//
// We use the *streaming* bridge endpoint (/api/v1/browser-delegate/stream): the
// browser pushes incremental chunks which we read as they arrive. That keeps
// the connection alive for long RAG + LLM answers, so the old fixed 30s
// round-trip timeout no longer applies — only the per-chunk idle timeout does.

const BRIDGE_URL = process.env.KLANGK_BRIDGE_URL;

/**
 * Read the current browser ID from klangk-browser-id.
 *
 * Call this per-request, not once at module load — the ID changes
 * when the user refreshes the browser or switches tabs.
 */
function getBrowserId(): string {
  try {
    return execSync("klangk-browser-id", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

function getWorkspaceToken(): string {
  try {
    return execSync("klangk-workspace-token", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

interface BridgeResult {
  text: string;
  error?: string;
}

/// POST to the streaming bridge and consume the NDJSON relay:
///   {"type":"chunk","delta":"..."}*  then
///   {"type":"done","result":{"status":"ok","result":"<text>"}}
///   | {"type":"error","error":"..."}
async function streamBridge(
  action: string,
  params: Record<string, unknown>,
  onUpdate?: (update: unknown) => void,
): Promise<BridgeResult> {
  const token = getWorkspaceToken();
  const resp = await fetch(`${BRIDGE_URL}/api/v1/browser-delegate/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ action, browser_id: getBrowserId(), ...params }),
  });
  if (!resp.ok) {
    const t = await resp.text().catch(() => "");
    return { text: "", error: `Bridge error (${resp.status}): ${t}` };
  }
  if (!resp.body) {
    return { text: await resp.text() };
  }

  const reader = (resp.body as ReadableStream<Uint8Array>).getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let acc = "";
  let finalText: string | null = null;
  let errText: string | undefined;

  const handleLine = (line: string) => {
    const s = line.trim();
    if (!s) return;
    let ev: any;
    try {
      ev = JSON.parse(s);
    } catch {
      return;
    }
    if (ev.type === "chunk") {
      acc += ev.delta ?? "";
      if (typeof onUpdate === "function") {
        try {
          onUpdate({ content: [{ type: "text", text: acc }] });
        } catch {
          /* ignore onUpdate contract mismatches across pi versions */
        }
      }
    } else if (ev.type === "done") {
      // result is the browser_response payload: {status:'ok', result:<text>}.
      const r = ev.result ?? {};
      if (r.error) errText = String(r.error);
      finalText = typeof r.result === "string" ? r.result : acc;
    } else if (ev.type === "error") {
      errText = String(ev.error ?? "bridge stream error");
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      handleLine(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
    }
  }
  if (buf.trim()) handleLine(buf);

  if (errText) return { text: finalText ?? acc, error: errText };
  return { text: finalText ?? acc };
}

function textResult(text: string) {
  return { content: [{ type: "text", text }], details: {} };
}

// Collapse whitespace/newlines so a value renders on a single line.
function oneLine(s: unknown): string {
  return (typeof s === "string" ? s : s == null ? "" : String(s))
    .replace(/\s+/g, " ")
    .trim();
}

// A single-line tool-call component. We can't value-import pi's Text from a raw
// .ts extension (pi-tui is nested under pi's global install, unreachable by Node
// ESM resolution from the extension dir — pi's own shipped extensions only
// `import type` from the pi package). So implement pi-tui's public `Component`
// interface directly. CRITICAL: render() MUST truncate to the given width — pi
// crashes if a rendered line exceeds the terminal width.
function callLine(text: string): {
  render: (w: number) => string[];
  invalidate: () => void;
} {
  return {
    render: (width: number) => {
      const w = typeof width === "number" && width > 4 ? width : 80;
      return [text.length <= w ? text : `${text.slice(0, w - 1)}…`];
    },
    invalidate: () => {},
  };
}

export default function (pi: any) {
  if (!BRIDGE_URL) return;

  pi.registerTool({
    name: "soliplex_list_rooms",
    description:
      "List the available Soliplex knowledge-base rooms (id, name, description) " +
      "for a server. The header also names other configured servers you can " +
      "target with the `server` argument on query/reply.",
    parameters: Type.Object({
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    async execute(_id: string, params: { server?: string }) {
      try {
        const { text, error } = await streamBridge("soliplex_list_rooms", {
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_list_rooms failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_get_room_info",
    description:
      "Get a Soliplex room's info — name, description, welcome message, and " +
      "its suggested prompts (the example questions to ask in that room) — " +
      "plus capability flags. Use the room_id from soliplex_list_rooms.",
    parameters: Type.Object({
      room_id: Type.String({
        description: "Room id (from soliplex_list_rooms).",
      }),
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      return callLine(
        `soliplex_get_room_info(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"})`,
      );
    },
    async execute(_id: string, params: { room_id: string; server?: string }) {
      try {
        const { text, error } = await streamBridge("soliplex_get_room_info", {
          room_id: params.room_id,
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_get_room_info failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_list_threads",
    description:
      "List the conversation threads in a Soliplex room (thread_id, name, " +
      "created) so you can resume one with soliplex_reply.",
    parameters: Type.Object({
      room_id: Type.String({
        description: "Room id (from soliplex_list_rooms).",
      }),
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      return callLine(
        `soliplex_list_threads(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"})`,
      );
    },
    async execute(_id: string, params: { room_id: string; server?: string }) {
      try {
        const { text, error } = await streamBridge("soliplex_list_threads", {
          room_id: params.room_id,
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_list_threads failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_query",
    description:
      "Ask a question to a Soliplex room (RAG + LLM). Starts a NEW conversation " +
      "thread and returns the answer. The result ends with the server + thread_id " +
      "— pass BOTH to soliplex_reply to continue the same conversation " +
      "(multi-turn). Long-running answers stream and will not time out.",
    parameters: Type.Object({
      room_id: Type.String({
        description: "Room id (from soliplex_list_rooms).",
      }),
      question: Type.String({ description: "The question to ask." }),
      server: Type.Optional(
        Type.String({
          description:
            "Soliplex server name (from soliplex_list_rooms). Omit for the " +
            "default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      return callLine(
        `soliplex_query(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"}, message: ${oneLine(a.question)})`,
      );
    },
    async execute(
      _id: string,
      params: { room_id: string; question: string; server?: string },
      _signal: AbortSignal | undefined,
      onUpdate: any,
    ) {
      try {
        const { text, error } = await streamBridge(
          "soliplex_query",
          {
            room_id: params.room_id,
            question: params.question,
            server: params.server,
          },
          onUpdate,
        );
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_query failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_query_all",
    description:
      "Ask ONE natural-language question of MANY Soliplex rooms at once and get " +
      "a single aggregated, per-target answer. This is a KNOWLEDGE query: each " +
      "target opens a NEW thread and answers from that room's indexed content " +
      "(RAG). It is NOT for room metadata (use soliplex_get_room_info) or files " +
      "(use soliplex_list_files / soliplex_get_file). Each target is " +
      '{server?, room}; omit server for the default; room: "*" fans out to ' +
      "every room on that server. The result has a `## server/room` section per " +
      "target with that room's answer (and its Sources) plus a thread_id you can " +
      "pass to soliplex_reply to continue that specific conversation. A failed " +
      "target (down, auth, unknown) shows an Error line while the others still " +
      "return. Fan-outs stream incrementally; a target silent longer than the " +
      "bridge idle timeout can still time out.",
    parameters: Type.Object({
      question: Type.String({
        description: "The question to ask every target.",
      }),
      targets: Type.Array(
        Type.Object({
          server: Type.Optional(
            Type.String({
              description: "Soliplex server name. Omit for the default server.",
            }),
          ),
          room: Type.String({
            description:
              'Room id (from soliplex_list_rooms), or "*" for all rooms.',
          }),
        }),
        { description: "The (server, room) targets to ask in parallel." },
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const n = Array.isArray(a.targets) ? a.targets.length : 0;
      return callLine(
        `soliplex_query_all(targets: ${n}, question: ${oneLine(a.question)})`,
      );
    },
    async execute(
      _id: string,
      params: {
        question: string;
        targets: Array<{ server?: string; room: string }>;
      },
      _signal: AbortSignal | undefined,
      onUpdate: any,
    ) {
      try {
        const { text, error } = await streamBridge(
          "soliplex_query_all",
          { question: params.question, targets: params.targets },
          onUpdate,
        );
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_query_all failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_reply",
    description:
      "Continue an existing Soliplex conversation thread (multi-turn). Use the " +
      "server + thread_id returned by a prior soliplex_query. The room keeps the " +
      "thread history, so earlier turns stay in context. Long answers stream.",
    parameters: Type.Object({
      room_id: Type.String({ description: "Room id of the thread." }),
      message: Type.String({ description: "The follow-up message." }),
      thread_id: Type.String({
        description: "thread_id from a prior soliplex_query result.",
      }),
      server: Type.Optional(
        Type.String({
          description:
            "Soliplex server name from the prior soliplex_query result. Must " +
            "match — omit only if that query used the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      return callLine(
        `soliplex_reply(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"}, ` +
          `message: ${oneLine(a.message)}, threadId: ${oneLine(a.thread_id)})`,
      );
    },
    async execute(
      _id: string,
      params: {
        room_id: string;
        message: string;
        thread_id: string;
        server?: string;
      },
      _signal: AbortSignal | undefined,
      onUpdate: any,
    ) {
      try {
        const { text, error } = await streamBridge(
          "soliplex_reply",
          {
            room_id: params.room_id,
            message: params.message,
            thread_id: params.thread_id,
            server: params.server,
          },
          onUpdate,
        );
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_reply failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_list_servers",
    description:
      "List the configured Soliplex servers (the names usable as the `server` " +
      "argument to soliplex_query/reply/list_rooms).",
    parameters: Type.Object({}),
    async execute() {
      try {
        const { text, error } = await streamBridge("soliplex_list_servers", {});
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_list_servers failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_add_server",
    description:
      "Register an additional Soliplex server so it can be queried by name. " +
      "After adding, the user may need to authenticate to it via the " +
      "'Connect to Soliplex' overlay (each server has its own login); no-auth " +
      "servers work immediately. The name is then usable as the `server` arg.",
    parameters: Type.Object({
      name: Type.String({
        description: "Short name for the server (used as the `server` arg).",
      }),
      url: Type.String({
        description:
          "Base URL of the Soliplex server, e.g. https://rag.example.net",
      }),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      return callLine(
        `soliplex_add_server(name: ${oneLine(a.name) || "?"}, url: ${oneLine(a.url) || "?"})`,
      );
    },
    async execute(_id: string, params: { name: string; url: string }) {
      try {
        const { text, error } = await streamBridge("soliplex_add_server", {
          name: params.name,
          url: params.url,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_add_server failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_remove_server",
    description:
      "Remove a previously added Soliplex server by name. The bundled " +
      "'default' server cannot be removed. Use soliplex_list_servers to see " +
      "the configured names.",
    parameters: Type.Object({
      name: Type.String({
        description:
          "Name of the server to remove (from soliplex_list_servers).",
      }),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      return callLine(
        `soliplex_remove_server(name: ${oneLine(a.name) || "?"})`,
      );
    },
    async execute(_id: string, params: { name: string }) {
      try {
        const { text, error } = await streamBridge("soliplex_remove_server", {
          name: params.name,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_remove_server failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_list_files",
    description:
      "List the files uploaded to a Soliplex room, or to a specific thread " +
      "within it (pass thread_id). Returns the filenames; fetch a file's " +
      "contents with soliplex_get_file. Only works when the room has " +
      "attachments enabled (check soliplex_get_room_info).",
    parameters: Type.Object({
      room_id: Type.String({
        description: "Room id (from soliplex_list_rooms).",
      }),
      thread_id: Type.Optional(
        Type.String({
          description:
            "Thread id to scope to a thread's uploads. Omit for room-level files.",
        }),
      ),
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      const tid = oneLine(a.thread_id);
      return callLine(
        `soliplex_list_files(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"}${tid ? `, threadId: ${tid}` : ""})`,
      );
    },
    async execute(
      _id: string,
      params: { room_id: string; thread_id?: string; server?: string },
    ) {
      try {
        const { text, error } = await streamBridge("soliplex_list_files", {
          room_id: params.room_id,
          thread_id: params.thread_id,
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_list_files failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_get_file",
    description:
      "Download a file uploaded to a Soliplex room (or thread). Text files are " +
      "returned inline; binary files are returned base64-encoded with a note " +
      "and their content type. Only works when the room has attachments enabled " +
      "(check soliplex_get_room_info).",
    parameters: Type.Object({
      room_id: Type.String({ description: "Room id of the file." }),
      filename: Type.String({
        description: "Filename (from soliplex_list_files).",
      }),
      thread_id: Type.Optional(
        Type.String({
          description:
            "Thread id if the file is thread-scoped. Omit for room files.",
        }),
      ),
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      const tid = oneLine(a.thread_id);
      return callLine(
        `soliplex_get_file(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"}, file: ${oneLine(a.filename) || "?"}${tid ? `, threadId: ${tid}` : ""})`,
      );
    },
    async execute(
      _id: string,
      params: {
        room_id: string;
        filename: string;
        thread_id?: string;
        server?: string;
      },
    ) {
      try {
        const { text, error } = await streamBridge("soliplex_get_file", {
          room_id: params.room_id,
          filename: params.filename,
          thread_id: params.thread_id,
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_get_file failed: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "soliplex_upload_file",
    description:
      "Upload a file to a Soliplex room (or thread). Provide EXACTLY ONE of " +
      "`content` (UTF-8 text) or `content_base64` (base64-encoded binary). " +
      "Optionally set content_type (e.g. text/markdown, application/pdf). Only " +
      "works when the room has attachments enabled (check soliplex_get_room_info).",
    parameters: Type.Object({
      room_id: Type.String({ description: "Room id to upload into." }),
      filename: Type.String({ description: "Name to store the file under." }),
      content: Type.Optional(
        Type.String({ description: "File contents as UTF-8 text." }),
      ),
      content_base64: Type.Optional(
        Type.String({
          description: "File contents as a base64 string (binary).",
        }),
      ),
      content_type: Type.Optional(
        Type.String({ description: "MIME type, e.g. text/markdown." }),
      ),
      thread_id: Type.Optional(
        Type.String({
          description:
            "Thread id to upload into a thread. Omit for room-level.",
        }),
      ),
      server: Type.Optional(
        Type.String({
          description: "Soliplex server name. Omit for the default server.",
        }),
      ),
    }),
    renderCall(args: any) {
      const a = args ?? {};
      const srv = oneLine(a.server);
      const tid = oneLine(a.thread_id);
      return callLine(
        `soliplex_upload_file(${srv ? `server: ${srv}, ` : ""}roomId: ${oneLine(a.room_id) || "?"}, file: ${oneLine(a.filename) || "?"}${tid ? `, threadId: ${tid}` : ""})`,
      );
    },
    async execute(
      _id: string,
      params: {
        room_id: string;
        filename: string;
        content?: string;
        content_base64?: string;
        content_type?: string;
        thread_id?: string;
        server?: string;
      },
    ) {
      try {
        const { text, error } = await streamBridge("soliplex_upload_file", {
          room_id: params.room_id,
          filename: params.filename,
          content: params.content,
          content_base64: params.content_base64,
          content_type: params.content_type,
          thread_id: params.thread_id,
          server: params.server,
        });
        return textResult(error ? `Error: ${error}` : text);
      } catch (e: any) {
        return textResult(`soliplex_upload_file failed: ${e?.message ?? e}`);
      }
    },
  });
}
