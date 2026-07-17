# @klangk/bridge

Browser-delegated tool calls for Klangk Pi extensions.

Routes requests through the Klangk backend to the user's browser, which executes them with its session credentials (cookies, OAuth tokens, etc.).

## Usage

```typescript
import { browserFetch, browserAction, isBridgeAvailable } from "@klangk/bridge";

// Fetch a URL using the browser's credentials
const result = await browserFetch("https://authenticated-api.com/data");
console.log(result.status, result.body);

// Trigger a browser-side action
await browserAction("celebrate");
await browserAction("beep");

// Check if the bridge is available
if (await isBridgeAvailable()) {
  // safe to use bridge functions
}
```

## Environment Variables

- `KLANGK_BRIDGE_URL` — URL of the Klangk backend (via the proxy). Set at container creation time.

The browser ID is read dynamically per-request via `klangk-browser-id` (not from an env var). This means bridge calls work after browser refresh and tab switches — the ID is always current.
