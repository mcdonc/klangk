/**
 * Dynamic model discovery for the klangk LLM proxy.
 *
 * Fetches available models from the proxy's /models endpoint (OpenAI
 * compatible) and registers them as the "llm-proxy" provider.  This
 * replaces the static single-model entry that setup_clankers writes
 * to models.json so Pi can see and switch between all models the
 * upstream LLM backend offers.
 *
 * The factory is async — Pi waits for it to complete before startup
 * finishes, so discovered models are available immediately.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

interface OpenAIModel {
  id: string;
  object?: string;
  created?: number;
  owned_by?: string;
}

interface OpenAIModelsResponse {
  data: OpenAIModel[];
}

export default async function (pi: ExtensionAPI) {
  const proxyUrl = process.env.KLANGK_LLM_PROXY_URL;
  const apiKey = process.env.KLANGK_WORKSPACE_TOKEN || "proxy";

  if (!proxyUrl) {
    return; // No LLM proxy configured
  }

  // Strip trailing slash and /v1 suffix for the baseUrl — Pi appends
  // its own path segments when making requests.
  const baseUrl = proxyUrl.replace(/\/+$/, "");

  let models: OpenAIModel[];
  try {
    const response = await fetch(`${baseUrl}/models`, {
      headers: { Authorization: `Bearer ${apiKey}` },
    });
    if (!response.ok) {
      console.error(
        `llm-proxy-models: failed to fetch models: ${response.status} ${response.statusText}`,
      );
      return;
    }
    const payload = (await response.json()) as OpenAIModelsResponse;
    models = payload.data ?? [];
  } catch (err) {
    console.error(`llm-proxy-models: failed to fetch models:`, err);
    return;
  }

  if (models.length === 0) {
    return;
  }

  // Filter out embedding and reranker models — they can't be used
  // for chat completions.
  const chatModels = models.filter((m) => {
    const lower = m.id.toLowerCase();
    return !lower.includes("embed") && !lower.includes("rerank");
  });

  if (chatModels.length === 0) {
    return;
  }

  pi.registerProvider("llm-proxy", {
    baseUrl,
    apiKey: "$KLANGK_WORKSPACE_TOKEN",
    api: "openai-completions",
    models: chatModels.map((m) => ({
      id: m.id,
      name: m.id,
      reasoning: false,
      input: ["text"] as ("text" | "image")[],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 4096,
    })),
  });

  console.error(
    `llm-proxy-models: registered ${chatModels.length} models from ${baseUrl}`,
  );
}
