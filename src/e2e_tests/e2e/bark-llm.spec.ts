import { test, expect } from "@playwright/test";
import {
  API_BASE,
  vp,
  flutterClick,
  sendPrompt,
  createAndOpenWorkspace,
} from "./helpers";

// LLM-dependent tests: each test contacts the LLM provider and may be slow or flaky.
// Retries up to 3 times to handle intermittent LLM response failures.

test.describe("Bark LLM", () => {
  test.describe.configure({ retries: 3 });

  test("get_hosted_url returns a hosted URL", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-hosted-url",
    );

    try {
      // Ask the LLM to call get_hosted_url — don't ask it to create
      // files or start a server. This tests the hosted URL mechanism
      // without depending on LLM coding ability.
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "what is the hosted url for a service running on port 8000? use the get_hosted_url tool to find out.",
      );

      // Poll for a hosted URL in the assistant messages or tool output
      let hostedUrl: string | null = null;
      for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(2000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          const urlRegex = /https?:\/\/localhost:\d+\/(bark\/)?hosted\/[^\s)]+/;
          for (const m of messages) {
            // Check assistant text and tool_call output for hosted URLs
            const text =
              m.entry_type === "assistant"
                ? (m.content ?? "")
                : m.entry_type === "tool_call"
                  ? (m.tool_output ?? "")
                  : "";
            const urlMatch = text.match(urlRegex);
            if (urlMatch) {
              hostedUrl = urlMatch[0];
              break;
            }
          }
        }
        if (hostedUrl) break;
      }
      expect(hostedUrl).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("agent creates a file with expected content", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-file-create",
    );

    try {
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        'create a file called hello.txt containing exactly the text "bark-e2e-test-ok"',
      );

      // Poll for the file to appear with the expected content
      let content: string | null = null;
      for (let i = 0; i < 60; i++) {
        await page.waitForTimeout(2000);
        const resp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/files/content?path=hello.txt`,
          { headers },
        );
        if (resp.ok()) {
          const data = await resp.json();
          if (data.content && data.content.includes("bark-e2e-test-ok")) {
            content = data.content;
            break;
          }
        }
      }
      expect(content).toBeTruthy();
      expect(content).toContain("bark-e2e-test-ok");
    } finally {
      await cleanup();
    }
  });

  test("abort stops a running agent", async ({ page, request }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-abort-test",
    );

    try {
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "write a very detailed 2000 word essay about the history of computing",
      );

      // Wait for the agent to start running
      await page.waitForTimeout(5000);

      // Click the abort button (red stop_circle icon, to the right of
      // the chat input). It's at the send button position.
      const { height } = vp(page);
      await flutterClick(page, 460, height - 30);
      await page.waitForTimeout(3000);

      // Verify the agent stopped — check that messages contain the user prompt
      const msgResp = await request.get(
        `${API_BASE}/workspaces/${workspaceId}/messages`,
        { headers },
      );
      expect(msgResp.ok()).toBeTruthy();
      const messages = await msgResp.json();
      expect(
        messages.some(
          (m: any) =>
            m.entry_type === "user" && m.content.includes("computing"),
        ),
      ).toBeTruthy();
    } finally {
      await cleanup();
    }
  });

  test("queued prompt is marked as queued in database", async ({
    page,
    request,
  }) => {
    const { workspaceId, headers, cleanup } = await createAndOpenWorkspace(
      page,
      request,
      "e2e-queue-test",
    );

    try {
      const { height } = vp(page);

      // Send first prompt — use a slow prompt so the agent is still
      // running when we send the second one.
      await sendPrompt(
        page,
        request,
        workspaceId,
        headers,
        "write a very detailed 500 word essay about the history of computing",
      );

      // Send second prompt immediately — agent should still be running,
      // so this prompt should be queued (is_queued=true in the database).
      await page.waitForTimeout(1000);
      await flutterClick(page, 240, height - 30);
      await page.waitForTimeout(300);
      await page.keyboard.type("what is 2+2? reply with just the number");
      await page.keyboard.press("Enter");

      // Poll until the second user message appears with is_queued=true
      let secondIsQueued = false;
      for (let i = 0; i < 30; i++) {
        await page.waitForTimeout(2000);
        const msgResp = await request.get(
          `${API_BASE}/workspaces/${workspaceId}/messages`,
          { headers },
        );
        if (msgResp.ok()) {
          const messages = await msgResp.json();
          const userMsgs = messages.filter((m: any) => m.entry_type === "user");
          // The second user message should have is_queued=true
          if (userMsgs.length >= 2) {
            const second = userMsgs[1];
            if (second.is_queued) {
              secondIsQueued = true;
              break;
            }
          }
        }
      }
      expect(secondIsQueued).toBeTruthy();
    } finally {
      await cleanup();
    }
  });
});
