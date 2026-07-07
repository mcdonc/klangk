import { test, expect } from "@playwright/test";
import { API_BASE } from "./helpers";
import * as fs from "fs";
import * as path from "path";

test.describe("Branding", () => {
  test("serves branding asset from customize dir", async ({ request }) => {
    // The E2E global setup creates <KLANGK_CUSTOMIZE_DIR>/branding/
    // before starting the server, so /branding is mounted.  Drop a
    // file there and verify it's served.  See #1360.
    const dataDir = process.env.KLANGK_E2E_DATA_DIR;
    expect(dataDir).toBeTruthy();
    const brandingDir = path.join(dataDir!, "customize", "branding");

    const filename = `e2e-test-${Date.now()}.txt`;
    const filePath = path.join(brandingDir, filename);
    const content = "branding-e2e-test-content";
    fs.writeFileSync(filePath, content);

    try {
      const resp = await request.get(`${API_BASE}/branding/${filename}`);
      expect(resp.status()).toBe(200);
      expect(await resp.text()).toBe(content);
    } finally {
      fs.unlinkSync(filePath);
    }
  });

  test("branding returns 404 for missing file", async ({ request }) => {
    const resp = await request.get(
      `${API_BASE}/branding/nonexistent-${Date.now()}.png`,
    );
    expect(resp.status()).toBe(404);
  });
});
