import { test, expect } from "@playwright/test";
import { execSync } from "child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "fs";
import jwt from "jsonwebtoken";
import { tmpdir } from "os";
import { join } from "path";
import { API_BASE, registerUser } from "./helpers";
import { cleanEnv } from "../e2e-env";

const JWT_SECRET = "e2e-test-secret";

test.describe("Token expiry", () => {
  test("expired workspace token returns distinct error", async ({
    request,
  }) => {
    // Create an expired workspace token
    const expiredToken = jwt.sign(
      { sub: "ws-test-123", purpose: "workspace" },
      JWT_SECRET,
      { algorithm: "HS256", expiresIn: -60 },
    );

    const resp = await request.get(
      `${API_BASE}/api/v1/auth/verify-workspace-token`,
      {
        headers: { Authorization: `Bearer ${expiredToken}` },
      },
    );

    expect(resp.status()).toBe(401);
    const body = await resp.json();
    expect(body.detail).toBe("Workspace token expired");
    // Verify the X-Token-Error header is set for nginx forwarding
    const tokenError = resp.headers()["x-token-error"];
    expect(tokenError).toBe("expired");
  });

  test("invalid workspace token returns distinct error", async ({
    request,
  }) => {
    const resp = await request.get(
      `${API_BASE}/api/v1/auth/verify-workspace-token`,
      {
        headers: { Authorization: "Bearer garbage-token" },
      },
    );

    expect(resp.status()).toBe(401);
    const body = await resp.json();
    expect(body.detail).toBe("Invalid workspace token");
    const tokenError = resp.headers()["x-token-error"];
    expect(tokenError).toBe("invalid");
  });

  test("klangkc shell with expired token shows session expired", async ({
    request,
  }) => {
    // Register a user and create a workspace so the shell has a target
    const email = `cli-expired-${Date.now()}@test.example.com`;
    const { token, headers } = await registerUser(request, email);
    const wsResp = await request.post(`${API_BASE}/api/v1/workspaces`, {
      headers,
      data: { name: `cli-expiry-${Date.now()}` },
    });
    expect(wsResp.ok()).toBeTruthy();
    const workspace = await wsResp.json();

    // Create an expired JWT
    const decoded = jwt.decode(token) as jwt.JwtPayload;
    const expiredToken = jwt.sign(
      { sub: decoded.sub, email: decoded.email, jti: "cli-expired-jti" },
      JWT_SECRET,
      { algorithm: "HS256", expiresIn: -60 },
    );

    // Write temporary CLI config and state with the expired token
    const tmpHome = mkdtempSync(join(tmpdir(), "klangk-cli-e2e-"));
    const configDir = join(tmpHome, ".config", "klangk");
    mkdirSync(configDir, { recursive: true });
    writeFileSync(
      join(configDir, "cli.yaml"),
      `servers:\n  test:\n    url: ${API_BASE}\n`,
    );
    writeFileSync(
      join(configDir, "state.yaml"),
      `active-server: "${API_BASE}"\n"${API_BASE}":\n  active-user: "${email}"\n  users:\n    "${email}":\n      token: ${expiredToken}\n`,
    );

    // Run klangkc shell — it should fail with a clear error
    try {
      execSync(`klangkc shell "${workspace.name}"`, {
        env: cleanEnv({ HOME: tmpHome }),
        encoding: "utf-8",
        timeout: 15_000,
      });
      // Should not succeed
      expect(false).toBe(true);
    } catch (e: any) {
      const output = (e.stderr || "") + (e.stdout || "");
      expect(output).toContain("Session expired");
      expect(output).toContain("klangkc login");
    }

    // Cleanup
    await request.delete(`${API_BASE}/api/v1/workspaces/${workspace.id}`, {
      headers,
    });
  });
});
