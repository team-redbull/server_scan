import { expect, test } from "@playwright/test";

test.describe("Maintenance", () => {
  test("enable and disable maintenance from the server detail page", async ({ page, request }) => {
    const listResponse = await request.get("/api/v1/servers?page_size=1");
    const { items } = (await listResponse.json()) as { items: { id: string }[] };
    const serverId = items[0]?.id;
    if (!serverId) {
      throw new Error("No seeded server available to test maintenance against.");
    }

    // Start from a known state regardless of what a previous run left
    // behind.
    await request.delete(`/api/v1/servers/${serverId}/maintenance`);

    await page.goto(`/servers/${serverId}`);
    await expect(page.getByText("Not in maintenance")).toBeVisible();

    await page.getByPlaceholder("Reason (optional)").fill("e2e test maintenance");
    await page.getByRole("button", { name: "Start maintenance" }).click();

    await expect(page.getByText("e2e test maintenance")).toBeVisible();
    await expect(page.getByRole("button", { name: "End maintenance" })).toBeVisible();

    await page.getByRole("button", { name: "End maintenance" }).click();
    await expect(page.getByText("Not in maintenance")).toBeVisible();

    // Reflected on the inventory list's "Maintenance only" filter too, not
    // just the detail page — enable it again and confirm the list filter
    // actually narrows to it.
    await page.getByRole("button", { name: "Start maintenance" }).click();
    await expect(page.getByRole("button", { name: "End maintenance" })).toBeVisible();

    await page.goto("/servers");
    // Not `.check()`: this checkbox's `onChange` drives a react-router
    // `setSearchParams` update, which can commit as a transition — the
    // DOM's `checked` property can genuinely flicker back to its old value
    // for a frame before settling, and `.check()`'s own built-in
    // before/after verification treats that flicker as "click had no
    // effect" even though the final state is correct. A plain click plus a
    // retrying `toBeChecked()` assertion waits out the settle instead.
    await page.getByLabel("Maintenance only").click();
    await expect(page.getByLabel("Maintenance only")).toBeChecked();
    await expect(page.getByRole("link", { name: /.+/ }).first()).toBeVisible();

    // Cleanup regardless of assertion outcome above.
    await request.delete(`/api/v1/servers/${serverId}/maintenance`);
  });
});
