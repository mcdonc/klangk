/**
 * @klangk/bridge — Browser-delegated tool calls for Klangk Pi extensions.
 *
 * Routes requests through the Klangk backend to the user's browser,
 * which executes them with its session credentials (cookies, OAuth tokens, etc.).
 */

function getConfig() {
  const bridgeUrl = process.env.KLANGK_BRIDGE_URL;
  const token = process.env.KLANGK_BRIDGE_TOKEN;
  const workspaceToken = process.env.KLANGK_WORKSPACE_TOKEN;
  if (!bridgeUrl) {
    throw new Error(
      "@klangk/bridge: KLANGK_BRIDGE_URL is not set. " +
        "Are you running inside a Klangk container?",
    );
  }
  if (!token) {
    throw new Error(
      "@klangk/bridge: KLANGK_BRIDGE_TOKEN is not set. " +
        "Are you running inside a Klangk container?",
    );
  }
  return {
    bridgeUrl: `${bridgeUrl}/api/browser-delegate`,
    token,
    workspaceToken: workspaceToken || null,
  };
}

/**
 * Fetch a URL using the user's browser session credentials.
 *
 * @param {string} url - The URL to fetch
 * @param {Object} [options] - Fetch options
 * @param {string} [options.method="GET"] - HTTP method
 * @param {Record<string, string>} [options.headers] - Request headers
 * @param {string} [options.body] - Request body
 * @returns {Promise<{status: number, headers: Record<string, string>, body: string}>}
 */
async function browserFetch(url, options = {}) {
  const { bridgeUrl, token, workspaceToken } = getConfig();

  const headers = { "Content-Type": "application/json" };
  if (workspaceToken) {
    headers["Authorization"] = `Bearer ${workspaceToken}`;
  }

  const resp = await fetch(bridgeUrl, {
    method: "POST",
    headers,
    body: JSON.stringify({
      action: "fetch",
      token,
      url,
      method: options.method || "GET",
      headers: options.headers || {},
      body: options.body || null,
    }),
  });

  if (!resp.ok) {
    let text;
    try {
      text = await resp.text();
    } catch {
      text = `(status ${resp.status})`;
    }
    throw new Error(
      `@klangk/bridge: fetch request failed (${resp.status}): ${text}`,
    );
  }

  return await resp.json();
}

/**
 * Trigger a browser-side action (e.g. celebrate, beep).
 *
 * @param {string} action - The action name
 * @param {Object} [payload] - Additional action-specific data
 * @returns {Promise<{status: string}>}
 */
async function browserAction(action, payload = {}) {
  const { bridgeUrl, token, workspaceToken } = getConfig();

  // Prevent payload from overwriting action or token
  const { action: _a, token: _t, ...safePayload } = payload;

  const headers = { "Content-Type": "application/json" };
  if (workspaceToken) {
    headers["Authorization"] = `Bearer ${workspaceToken}`;
  }

  const resp = await fetch(bridgeUrl, {
    method: "POST",
    headers,
    body: JSON.stringify({
      action,
      token,
      ...safePayload,
    }),
  });

  if (!resp.ok) {
    let text;
    try {
      text = await resp.text();
    } catch {
      text = `(status ${resp.status})`;
    }
    throw new Error(
      `@klangk/bridge: action '${action}' failed (${resp.status}): ${text}`,
    );
  }

  return await resp.json();
}

/**
 * Check whether the browser bridge is available.
 * @returns {Promise<boolean>}
 */
async function isBridgeAvailable() {
  const bridgeUrl = process.env.KLANGK_BRIDGE_URL;
  const token = process.env.KLANGK_BRIDGE_TOKEN;
  if (!bridgeUrl || !token) return false;
  try {
    const resp = await fetch(`${bridgeUrl}/health`, {
      signal: AbortSignal.timeout(2000),
    });
    return resp.ok;
  } catch {
    return false;
  }
}

module.exports = { browserFetch, browserAction, isBridgeAvailable };
