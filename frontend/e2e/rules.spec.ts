import { expect, test } from "@playwright/test";

/**
 * The Rules & Policies page is read-only by design: classification rules
 * and health policies ship with the platform and are seeded at startup,
 * so that two installations classify and score identically. These specs
 * replace the editor flows that used to live here — there is no longer a
 * create, edit, enable/disable or delete path in the UI to exercise.
 */
test.describe("Rules & Policies", () => {
  test("shows both rules and policies on one page", async ({ page }) => {
    await page.goto("/rules");

    await expect(page.getByRole("heading", { name: "Rules & Policies" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Classification rules" }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Health policies" })).toBeVisible();

    // Seeded system rules and policies exist in any bootstrapped
    // deployment, so both tables have rows without this spec creating any.
    await expect(page.locator("table").first().locator("tbody tr")).not.toHaveCount(0);
    await expect(page.locator("table").nth(1).locator("tbody tr")).not.toHaveCount(0);
  });

  test("offers nothing to click that would change the configuration", async ({ page }) => {
    await page.goto("/rules");
    await expect(page.getByRole("heading", { name: "Health policies" })).toBeVisible();

    // The nav's own links are the only ones on the page; nothing inside
    // the two sections is interactive.
    await expect(page.getByRole("main").getByRole("button")).toHaveCount(0);
    await expect(page.getByRole("main").getByRole("link")).toHaveCount(0);
  });

  test("is reachable from the top-level navigation", async ({ page }) => {
    await page.goto("/servers");

    await page.getByRole("link", { name: "Rules & Policies" }).click();

    await expect(page).toHaveURL(/\/rules$/);
  });
});
