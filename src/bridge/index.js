/**
 * @klangk/bridge — Browser-delegated tool calls for Klangk Pi extensions.
 *
 * Routes requests through the Klangk backend to the user's browser,
 * which executes them with its session credentials (cookies, OAuth tokens, etc.).
 *
 * The browser ID is read dynamically per-request via `klangk-browser-id`
 * (which reads from tmux's global environment, updated on every browser
 * attach/reattach).  Do NOT cache it — it changes on browser refresh.
 */

const { execSync } = require("child_process");

/**
 * Read the current browser ID from klangk-browser-id.
 * Returns empty string if unavailable.
 */
function getBrowserId() {
  try {
    return execSync("klangk-browser-id", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

/**
 * Read the current workspace token from klangk-workspace-token.
 * Returns empty string if unavailable.
 */
function getWorkspaceToken() {
  try {
    return execSync("klangk-workspace-token", { encoding: "utf-8" }).trim();
  } catch {
    return "";
  }
}

function getConfig() {
  const bridgeUrl = process.env.KLANGK_BRIDGE_URL;
  const workspaceToken = getWorkspaceToken();
  if (!bridgeUrl) {
    throw new Error(
      "@klangk/bridge: KLANGK_BRIDGE_URL is not set. " +
        "Are you running inside a Klangk container?",
    );
  }
  const browserId = getBrowserId();
  if (!browserId) {
    throw new Error(
      "@klangk/bridge: klangk-browser-id returned nothing. " +
        "Is a browser tab connected?",
    );
  }
  return {
    bridgeUrl: `${bridgeUrl}/api/v1/browser-delegate`,
    browserId,
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
  const { bridgeUrl, browserId, workspaceToken } = getConfig();

  const headers = { "Content-Type": "application/json" };
  if (workspaceToken) {
    headers["Authorization"] = `Bearer ${workspaceToken}`;
  }

  const resp = await fetch(bridgeUrl, {
    method: "POST",
    headers,
    body: JSON.stringify({
      action: "fetch",
      browser_id: browserId,
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
  const { bridgeUrl, browserId, workspaceToken } = getConfig();

  // Prevent payload from overwriting action or browser_id
  const { action: _a, browser_id: _b, ...safePayload } = payload;

  const headers = { "Content-Type": "application/json" };
  if (workspaceToken) {
    headers["Authorization"] = `Bearer ${workspaceToken}`;
  }

  const resp = await fetch(bridgeUrl, {
    method: "POST",
    headers,
    body: JSON.stringify({
      action,
      browser_id: browserId,
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
  if (!bridgeUrl) return false;
  const browserId = getBrowserId();
  if (!browserId) return false;
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
