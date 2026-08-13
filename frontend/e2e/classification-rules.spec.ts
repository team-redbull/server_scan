import { expect, test } from "@playwright/test";

import { deleteClassificationRuleByName, labeledField, uniqueName } from "./helpers.js";

test.describe("Classification rules", () => {
  let ruleName: string;

  test.beforeEach(() => {
    ruleName = uniqueName("rule");
  });

  test.afterEach(async ({ request }) => {
    await deleteClassificationRuleByName(request, ruleName);
  });

  test("create, preview, disable, and delete a rule", async ({ page }) => {
    await page.goto("/classification-rules");
    await page.getByRole("link", { name: "New Rule" }).click();
    await expect(page.getByRole("heading", { name: "New Classification Rule" })).toBeVisible();

    // `labeledField`, not `getByLabel` — see its docstring for the
    // "SourceSITE_CUSTOMMANAGER_CUSTOMVENDOR_CUSTOM…" concatenation quirk
    // that made `getByLabel("Vendor")` collide with the Source select.
    await labeledField(page, "Name").fill(ruleName);
    await labeledField(page, "Source").selectOption("VENDOR_CUSTOM");
    await labeledField(page, "Vendor").selectOption("dell");
    await labeledField(page, "Pattern (regex)").fill("^ocp-dell-.*");

    // The live preview panel hits the real preview endpoint — wait for it
    // rather than asserting immediately, since it's debounced.
    const previewResponse = page.waitForResponse((res) =>
      res.url().includes("/api/v1/classification-rules/preview"),
    );
    await previewResponse;
    await expect(page.getByText(/server(s)? match/)).toBeVisible();

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page).toHaveURL(/\/classification-rules$/);
    await expect(page.getByRole("link", { name: ruleName })).toBeVisible();

    // Edit: disable it.
    await page.getByRole("link", { name: ruleName }).click();
    await expect(page.getByRole("heading", { name: "Edit Classification Rule" })).toBeVisible();
    await labeledField(page, "Enabled").uncheck();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page).toHaveURL(/\/classification-rules$/);

    const row = page.locator("tr", { has: page.getByRole("link", { name: ruleName }) });
    await expect(row.getByText("disabled")).toBeVisible();

    // Delete.
    page.once("dialog", (dialog) => dialog.accept());
    await row.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByRole("link", { name: ruleName })).toHaveCount(0);
  });
});
