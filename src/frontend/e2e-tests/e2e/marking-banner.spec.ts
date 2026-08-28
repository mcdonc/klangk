import { test, expect } from "@playwright/test";
import { registerUser, createWorkspace, openWorkspace } from "./helpers";

// #2768: the STIG layout properties of the classification marking —
// markings "at the top and the bottom of screens", never displaced by
// transient banners. Asserted against the real rendered page (viewport
// geometry, not widget-tree internals): one strip pinned to the very top
// of the viewport, one to the very bottom, both carrying the full label.
test.describe("classification marking banner", () => {
  test("renders the marking pinned to the top and bottom of the viewport", async ({
    page,
    request,
  }) => {
    const email = `marking-owner-${Date.now()}@test.example.com`;
    const { headers } = await registerUser(request, email);
    const { workspaceId, cleanup } = await createWorkspace(
      request,
      headers,
      "MarkingTest",
      { classification_banner: "SECRET//NOFORN" },
    );

    try {
      await openWorkspace(page, email, workspaceId);

      // Two strips carry the full marking: top and bottom.
      const banners = page.getByText("SECRET//NOFORN", { exact: true });
      await expect(banners).toHaveCount(2);

      const viewportHeight = page.viewportSize()?.height ?? 0;
      expect(viewportHeight).toBeGreaterThan(0);

      const top = (await banners.nth(0).boundingBox())!;
      const bottom = (await banners.nth(1).boundingBox())!;

      // Pinned: the top strip starts at (or within a few pixels of) the
      // viewport's top edge; the bottom strip ends at the bottom edge.
      // The full label must be inside the viewport (no ellipsized
      // marking — an unreadable marking is not a marking).
      expect(top.y).toBeGreaterThanOrEqual(0);
      expect(top.y).toBeLessThan(48);
      expect(bottom.y + bottom.height).toBeLessThanOrEqual(viewportHeight);
      expect(viewportHeight - (bottom.y + bottom.height)).toBeLessThan(48);
    } finally {
      await cleanup();
    }
  });
});
