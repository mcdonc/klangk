/**
 * Pi OTEL Telemetry Extension
 *
 * Exports OpenTelemetry traces AND metrics for pi coding agent sessions.
 *
 * Configuration via environment variables:
 *   OTEL_EXPORTER_OTLP_ENDPOINT         - Base OTLP HTTP endpoint (default: http://localhost:4318)
 *   OTEL_EXPORTER_OTLP_TRACES_ENDPOINT  - Trace endpoint override
 *   OTEL_EXPORTER_OTLP_METRICS_ENDPOINT - Metrics endpoint override
 *   OTEL_SERVICE_NAME                   - Service name (default: pi-coding-agent)
 *   PI_OTEL_ENABLED                     - Enable/disable (default: true)
 *   PI_OTEL_DEBUG                       - Log spans to console (default: false)
 *   PI_OTEL_USER_EMAIL                  - Override user email (default: git config user.email)
 *   PI_OTEL_USER_NAME                   - Override user name (default: git config user.name)
 *
 * Traces:
 *   session (root span)
 *   └── agent.prompt (per user prompt)
 *       └── agent.turn (per LLM turn)
 *           ├── tool.{name} (per tool call)
 *           └── llm.request (LLM API call)
 *
 * Metrics:
 *   pi.tokens.input        - counter, total input tokens
 *   pi.tokens.output       - counter, total output tokens
 *   pi.tool.calls          - counter, tool invocations (by tool.name)
 *   pi.tool.errors         - counter, failed tool invocations (by tool.name)
 *   pi.tool.duration       - histogram, tool execution time in ms (by tool.name)
 *   pi.turns               - counter, LLM turns
 *   pi.prompts             - counter, user prompts (agent starts)
 *   pi.session.duration    - histogram, session duration in seconds
 */

import type {
  ExtensionAPI,
  ExtensionContext,
} from "@mariozechner/pi-coding-agent";

import { execSync } from "child_process";
import { hostname, userInfo } from "os";
import {
  trace,
  context,
  SpanStatusCode,
  type Span,
  type Tracer,
} from "@opentelemetry/api";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  BatchSpanProcessor,
  ConsoleSpanExporter,
} from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import {
  MeterProvider,
  PeriodicExportingMetricReader,
  ConsoleMetricExporter,
} from "@opentelemetry/sdk-metrics";
import { Resource } from "@opentelemetry/resources";
import {
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION,
} from "@opentelemetry/semantic-conventions";

export default function (pi: ExtensionAPI) {
  const enabled = process.env.PI_OTEL_ENABLED !== "false";
  if (!enabled) return;

  const debug = process.env.PI_OTEL_DEBUG === "true";
  const endpoint =
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT || "http://localhost:4318";
  const tracesEndpoint = resolveOtlpHttpEndpoint(
    process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT || endpoint,
    "traces",
  );
  const serviceName = process.env.OTEL_SERVICE_NAME || "pi-coding-agent";
  const metricInterval = parseInt(
    process.env.OTEL_METRIC_EXPORT_INTERVAL || "10000",
    10,
  );
  const metricsEndpoint = resolveOtlpHttpEndpoint(
    process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT || endpoint,
    "metrics",
  );

  // --- Resolve account identity ---
  const account = resolveAccount();

  // --- Resource setup (shared by traces & metrics) ---
  const resourceAttrs: Record<string, string> = {
    [ATTR_SERVICE_NAME]: serviceName,
    [ATTR_SERVICE_VERSION]: "1.0.0",
    "pi.extension": "otel-telemetry",
    "host.name": account.hostname,
    "user.name": account.userName,
  };
  if (account.email) resourceAttrs["user.email"] = account.email;
  if (account.gitName) resourceAttrs["user.full_name"] = account.gitName;

  const envAttrs = process.env.OTEL_RESOURCE_ATTRIBUTES;
  if (envAttrs) {
    for (const pair of envAttrs.split(",")) {
      const eq = pair.indexOf("=");
      if (eq > 0) {
        resourceAttrs[pair.slice(0, eq).trim()] = pair.slice(eq + 1).trim();
      }
    }
  }

  const resource = new Resource(resourceAttrs);

  // --- Common metric attributes (promoted to Prometheus labels) ---
  // Resource attributes are NOT auto-promoted by Mimir except service.name → job.
  // To filter by user/machine in Grafana, we pass these as metric-level attributes.
  const commonMetricAttrs: Record<string, string> = {};
  if (resourceAttrs["user.name"]) {
    commonMetricAttrs["user.name"] = resourceAttrs["user.name"];
  }
  if (resourceAttrs["environment"]) {
    commonMetricAttrs["environment"] = resourceAttrs["environment"];
  }
  commonMetricAttrs["host.name"] = hostname();

  // --- Trace provider ---
  const traceProvider = new NodeTracerProvider({ resource });

  traceProvider.addSpanProcessor(
    new BatchSpanProcessor(new OTLPTraceExporter({ url: tracesEndpoint })),
  );
  if (debug) {
    traceProvider.addSpanProcessor(
      new BatchSpanProcessor(new ConsoleSpanExporter()),
    );
  }
  // Don't use traceProvider.register() — it sets the global provider which
  // can only be done once per process. On /reload the extension re-runs but
  // the global is already set, so the new provider's spans wouldn't export.
  // Instead, get the tracer directly from this provider instance.
  const tracer: Tracer = traceProvider.getTracer("pi-otel-extension", "1.0.0");

  // --- Metric provider ---
  const metricReader = new PeriodicExportingMetricReader({
    exporter: new OTLPMetricExporter({ url: metricsEndpoint }),
    exportIntervalMillis: metricInterval,
  });

  const readers: PeriodicExportingMetricReader[] = [metricReader];

  if (debug) {
    const debugReader = new PeriodicExportingMetricReader({
      exporter: new ConsoleMetricExporter(),
      exportIntervalMillis: metricInterval,
    });
    readers.push(debugReader);
  }

  const meterProvider = new MeterProvider({ resource, readers });
  const meter = meterProvider.getMeter("pi-otel-extension", "1.0.0");

  // --- Metrics ---
  const tokensInputCounter = meter.createCounter("pi.tokens.input", {
    description: "Total input tokens consumed",
    unit: "tokens",
  });
  const tokensOutputCounter = meter.createCounter("pi.tokens.output", {
    description: "Total output tokens produced",
    unit: "tokens",
  });
  const toolCallsCounter = meter.createCounter("pi.tool.calls", {
    description: "Total tool invocations",
  });
  const toolErrorsCounter = meter.createCounter("pi.tool.errors", {
    description: "Total failed tool invocations",
  });
  const toolDurationHistogram = meter.createHistogram("pi.tool.duration", {
    description: "Tool execution duration",
    unit: "ms",
  });
  const turnsCounter = meter.createCounter("pi.turns", {
    description: "Total LLM turns",
  });
  const promptsCounter = meter.createCounter("pi.prompts", {
    description: "Total user prompts (agent starts)",
  });
  const sessionDurationHistogram = meter.createHistogram(
    "pi.session.duration",
    {
      description: "Session duration",
      unit: "s",
    },
  );

  // --- Span tracking ---
  let sessionSpan: Span | undefined;
  let sessionCtx = context.active();
  let agentSpan: Span | undefined;
  let agentCtx = context.active();
  let turnSpan: Span | undefined;
  let turnCtx = context.active();
  const toolSpans = new Map<
    string,
    { span: Span; ctx: typeof agentCtx; startTime: number }
  >();

  let turnCount = 0;
  let totalToolCalls = 0;
  let totalTokensIn = 0;
  let totalTokensOut = 0;
  let sessionStartTime = 0;
  let currentModel = "";

  // --- Session lifecycle ---
  pi.on("session_start", async (_event, ctx) => {
    sessionStartTime = Date.now();
    sessionSpan = tracer.startSpan("session", {
      attributes: {
        "session.id": ctx.sessionManager.getSessionFile() ?? "ephemeral",
        "session.cwd": ctx.cwd,
        "user.email": account.email,
        "user.name": account.userName,
        "user.full_name": account.gitName,
        "host.name": account.hostname,
      },
    });
    sessionCtx = trace.setSpan(context.active(), sessionSpan);
    turnCount = 0;
    totalToolCalls = 0;
    totalTokensIn = 0;
    totalTokensOut = 0;

    if (debug) {
      ctx.ui.setStatus("otel", ctx.ui.theme.fg("dim", "📡 OTEL active"));
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    // Record session duration metric
    if (sessionStartTime > 0) {
      const durationSec = (Date.now() - sessionStartTime) / 1000;
      sessionDurationHistogram.record(durationSec, commonMetricAttrs);
    }

    if (sessionSpan) {
      sessionSpan.setAttribute("session.turns", turnCount);
      sessionSpan.setAttribute("session.tool_calls", totalToolCalls);
      sessionSpan.setAttribute("session.tokens.input", totalTokensIn);
      sessionSpan.setAttribute("session.tokens.output", totalTokensOut);
      sessionSpan.setStatus({ code: SpanStatusCode.OK });
      sessionSpan.end();
      sessionSpan = undefined;
    }

    // Flush all pending data before exit. Export failures should not break pi.
    for (const [label, op] of [
      ["metrics forceFlush", () => meterProvider.forceFlush()],
      ["traces forceFlush", () => traceProvider.forceFlush()],
      ["metrics shutdown", () => meterProvider.shutdown()],
      ["traces shutdown", () => traceProvider.shutdown()],
    ] as const) {
      try {
        await op();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (debug) {
          ctx.ui.notify(`OTEL ${label} failed: ${message}`, "warning");
        } else {
          console.error(`[pi-otel-telemetry] ${label} failed: ${message}`);
        }
      }
    }
  });

  // --- Agent (per user prompt) ---
  pi.on("agent_start", async (_event, _ctx) => {
    promptsCounter.add(1, commonMetricAttrs);

    agentSpan = tracer.startSpan(
      "agent.prompt",
      {
        attributes: {
          "agent.turn_count": 0,
        },
      },
      sessionCtx,
    );
    agentCtx = trace.setSpan(sessionCtx, agentSpan);
  });

  pi.on("agent_end", async (event) => {
    if (agentSpan) {
      agentSpan.setAttribute(
        "agent.messages_count",
        event.messages?.length ?? 0,
      );
      agentSpan.setStatus({ code: SpanStatusCode.OK });
      agentSpan.end();
      agentSpan = undefined;
    }
  });

  // --- Turn (per LLM call + tool execution) ---
  pi.on("turn_start", async (event) => {
    turnCount++;
    turnsCounter.add(1, commonMetricAttrs);

    turnSpan = tracer.startSpan(
      "agent.turn",
      {
        attributes: {
          "turn.index": event.turnIndex,
          "turn.number": turnCount,
        },
      },
      agentCtx,
    );
    turnCtx = trace.setSpan(agentCtx, turnSpan);
  });

  pi.on("turn_end", async (event) => {
    if (turnSpan) {
      const toolResultCount = event.toolResults?.length ?? 0;
      turnSpan.setAttribute("turn.tool_results", toolResultCount);

      // Extract token usage from the assistant message
      // AssistantMessage has .usage with { input, output, cacheRead, cacheWrite, totalTokens }
      const msg = event.message as any;

      if (msg?.role === "assistant" && msg?.usage) {
        const inputTokens = msg.usage.input ?? 0;
        const outputTokens = msg.usage.output ?? 0;
        const cacheRead = msg.usage.cacheRead ?? 0;
        const cacheWrite = msg.usage.cacheWrite ?? 0;
        turnSpan.setAttribute("llm.usage.input_tokens", inputTokens);
        turnSpan.setAttribute("llm.usage.output_tokens", outputTokens);
        turnSpan.setAttribute("llm.usage.cache_read_tokens", cacheRead);
        turnSpan.setAttribute("llm.usage.cache_write_tokens", cacheWrite);
        turnSpan.setAttribute(
          "llm.usage.total_tokens",
          msg.usage.totalTokens ?? 0,
        );
        totalTokensIn += inputTokens + cacheRead + cacheWrite;
        totalTokensOut += outputTokens;

        // Record token metrics (no model label to avoid series fragmentation)
        tokensInputCounter.add(
          inputTokens + cacheRead + cacheWrite,
          commonMetricAttrs,
        );
        tokensOutputCounter.add(outputTokens, commonMetricAttrs);
      }

      turnSpan.setStatus({ code: SpanStatusCode.OK });
      turnSpan.end();
      turnSpan = undefined;
    }
  });

  // --- Tool execution ---
  pi.on("tool_execution_start", async (event) => {
    totalToolCalls++;
    toolCallsCounter.add(1, {
      "tool.name": event.toolName,
      ...commonMetricAttrs,
    });

    const span = tracer.startSpan(
      `tool.${event.toolName}`,
      {
        attributes: {
          "tool.name": event.toolName,
          "tool.call_id": event.toolCallId,
          "tool.args_summary": summarizeArgs(event.toolName, event.args),
        },
      },
      turnCtx,
    );
    const spanCtx = trace.setSpan(turnCtx, span);
    toolSpans.set(event.toolCallId, {
      span,
      ctx: spanCtx,
      startTime: Date.now(),
    });
  });

  pi.on("tool_execution_end", async (event) => {
    const entry = toolSpans.get(event.toolCallId);
    if (entry) {
      const durationMs = Date.now() - entry.startTime;
      const attrs = { "tool.name": event.toolName, ...commonMetricAttrs };

      // Record tool duration metric
      toolDurationHistogram.record(durationMs, attrs);

      if (event.isError) {
        toolErrorsCounter.add(1, attrs);
        entry.span.setStatus({
          code: SpanStatusCode.ERROR,
          message: "Tool execution failed",
        });
      } else {
        entry.span.setStatus({ code: SpanStatusCode.OK });
      }

      entry.span.setAttribute("tool.is_error", event.isError ?? false);
      entry.span.setAttribute("tool.duration_ms", durationMs);
      entry.span.end();
      toolSpans.delete(event.toolCallId);
    }
  });

  // --- Model selection ---
  pi.on("model_select", async (event) => {
    currentModel = `${event.model.provider}/${event.model.id}`;

    if (sessionSpan) {
      sessionSpan.setAttribute("llm.model", currentModel);

      if (event.previousModel) {
        const prevId = `${event.previousModel.provider}/${event.previousModel.id}`;
        sessionSpan.addEvent("model.changed", {
          "model.previous": prevId,
          "model.current": currentModel,
          "model.source": event.source,
        });
      }
    }
  });

  // --- Compaction ---
  pi.on("session_compact", async (event) => {
    if (sessionSpan) {
      sessionSpan.addEvent("session.compacted", {
        "compaction.from_extension": event.fromExtension ?? false,
      });
    }
  });

  // --- Provider request (for timing LLM calls) ---
  pi.on("before_provider_request", (event) => {
    if (turnSpan) {
      turnSpan.addEvent("llm.request", {
        "llm.payload_size": JSON.stringify(event.payload).length,
      });
    }
  });
}

/**
 * Create a brief summary of tool arguments for span attributes.
 * Avoids dumping large content into traces.
 */
function summarizeArgs(toolName: string, args: any): string {
  if (!args) return "";

  switch (toolName) {
    case "bash":
      return truncate(args.command ?? "", 200);
    case "read":
      return args.path ?? "";
    case "write":
      return args.path ?? "";
    case "edit":
      return args.path ?? "";
    default:
      try {
        return truncate(JSON.stringify(args), 200);
      } catch {
        return "[unserializable]";
      }
  }
}

function truncate(str: string, maxLen: number): string {
  return str.length > maxLen ? str.slice(0, maxLen) + "…" : str;
}

/**
 * Resolve account identity from env vars, git config, and OS.
 * Priority: env vars > git config > OS defaults.
 */
function resolveAccount(): {
  email: string;
  gitName: string;
  userName: string;
  hostname: string;
} {
  const email = process.env.PI_OTEL_USER_EMAIL || gitConfig("user.email") || "";
  const gitName = process.env.PI_OTEL_USER_NAME || gitConfig("user.name") || "";
  const userName = userInfo().username;
  const host = hostname();

  return { email, gitName, userName, hostname: host };
}

function gitConfig(key: string): string {
  try {
    return execSync(`git config --global ${key}`, {
      encoding: "utf-8",
      timeout: 2000,
    }).trim();
  } catch {
    return "";
  }
}

function resolveOtlpHttpEndpoint(
  endpoint: string,
  signal: "traces" | "metrics",
): string {
  const trimmed = endpoint.replace(/\/+$/, "");
  if (trimmed.endsWith(`/v1/${signal}`)) return trimmed;
  if (trimmed.endsWith("/v1/traces") || trimmed.endsWith("/v1/metrics"))
    return trimmed;
  return `${trimmed}/v1/${signal}`;
}
