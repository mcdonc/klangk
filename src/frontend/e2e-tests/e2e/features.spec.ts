// Feature-artifact e2e — what the frontend actually sees at boot (#1666).
//
// After `klangk:flutter-build` emits features.json next to index.html and
// klangkd serves the frontend dir at /, the browser (at boot) fetches two
// artifacts: the sibling features.json (per-feature metadata + the
// default-on set) and /api/v1/config (the deploy's active-feature knob +
// the frontend-scope config values). This spec asserts both are served and
// carry the expected feature set — proving the build → serve → API chain
// is intact end-to-end.
//
// Runs in the chromium-api project (API/sibling-file fetches, no browser
// rendering). The full "does the UI render the feature" path is heavier
// (features surface as Pi extensions / app-bar actions inside a workspace)
// and lives elsewhere; this is the artifact-visibility gate.
//
// Keep EXPECTED_FEATURES in sync with scripts/tests/test_build_pipeline.py
// and the checked-in features.yaml.

import { test, expect } from "@playwright/test";
import { API_BASE } from "./helpers";

// The four features with a klangk/ Dart package — the set that appears in
// features.json's features[] list (TS-only features are absent from the
// manifest; they're baked into the workspace image and always-on, #1655).
const EXPECTED_FEATURES = ["celebrate", "beep", "boingball", "git-credential"];

test.describe("feature artifacts visible to the frontend", () => {
  test("features.json is served at /features.json with the expected set", async ({
    request,
  }) => {
    const resp = await request.get(`${API_BASE}/features.json`);
    expect(
      resp.ok(),
      "features.json must be served as a frontend sibling",
    ).toBeTruthy();
    const manifest = await resp.json();

    // Top-level shape — the runtime Features._read_manifest() contract.
    expect(manifest).toHaveProperty("features");
    expect(manifest).toHaveProperty("defaults");
    expect(manifest).toHaveProperty("container_env_keys");

    const featureNames = (manifest.features as Array<{ name: string }>).map(
      (f) => f.name,
    );
    // Every expected Dart feature is in the served manifest. (We don't assert
    // exact equality — a future PR may add a dormant compiled-in feature like
    // soliplex (#1664) without breaking this test. The build-pipeline unit
    // test locks the exact set; this asserts the subset the frontend cares
    // about is present after the real build + serve.)
    for (const name of EXPECTED_FEATURES) {
      expect(featureNames, `features.json missing ${name}`).toContain(name);
    }

    // defaults is a list of strings (the frontend reads it as the default-on
    // set when KLANGK_FEATURES_ENABLE is unset).
    expect(Array.isArray(manifest.defaults)).toBeTruthy();
    for (const d of manifest.defaults as unknown[]) {
      expect(typeof d).toBe("string");
    }
  });

  test("/api/v1/config surfaces frontend-scope keys from the manifest", async ({
    request,
  }) => {
    // /api/v1/config is public (the login page fetches it pre-auth for
    // oidc_providers / login_banner), so no auth needed.
    const resp = await request.get(`${API_BASE}/api/v1/config`);
    expect(resp.ok()).toBeTruthy();
    const config = await resp.json();

    // boingball declares KLANGK_FEATURE_BOING_SPEED with scope: frontend —
    // the server strips the KLANGK_FEATURE_ prefix and lowercases the suffix
    // to produce the /api/config key (so the JSON key is `boing_speed`, not
    // `klangk_feature_boing_speed`). The prefix is the feature-config
    // namespace from #1662; the suffix is the feature-owned name. The key's
    // *presence* is what matters here: it proves the server read
    // features.json and bridged the frontend-scope config block through to
    // /api/config. The feature reads exactly this stripped-lowercased name
    // (features/boingball/klangk/lib/feature.dart), so a drift here breaks
    // the runtime contract between server and feature.
    expect(
      config,
      "boing_speed missing — server didn't bridge boingball's frontend config",
    ).toHaveProperty("boing_speed");

    // git-credential's KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID has scope:
    // container, so it must NOT appear in /api/config (frontend-scope only).
    // Guards against a scope-classification regression leaking container
    // env into the browser. Stripped-lowercased form — the server strips
    // KLANGK_FEATURE_ and lowercases the suffix for /api/config keys.
    expect(config).not.toHaveProperty("github_oauth_client_id");
  });

  test("/api/v1/config omits features_enable when KLANGK_FEATURES_ENABLE unset", async ({
    request,
  }) => {
    // The e2e server is launched without KLANGK_FEATURES_ENABLE, so the
    // frontend must fall back to the manifest's defaults list. Asserting the
    // key is absent (not just falsy) locks the canonical-semantics contract
    // from #1655: unset → frontend uses defaults; set → exactly that list.
    const resp = await request.get(`${API_BASE}/api/v1/config`);
    expect(resp.ok()).toBeTruthy();
    const config = await resp.json();
    expect(config).not.toHaveProperty("features_enable");
  });
});
