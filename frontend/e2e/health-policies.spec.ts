import { expect, test } from "@playwright/test";

import { deleteHealthPolicyByName, labeledField, uniqueName } from "./helpers.js";

test.describe("Health policies", () => {
  let firstName: string;
  let secondName: string;
  let policyKey: string;

  test.beforeEach(() => {
    firstName = uniqueName("policy-a");
    secondName = uniqueName("policy-b");
    policyKey = uniqueName("shadow-key");
  });

  test.afterEach(async ({ request }) => {
    await deleteHealthPolicyByName(request, firstName);
    await deleteHealthPolicyByName(request, secondName);
  });

  async function createPolicy(page: import("@playwright/test").Page, name: string) {
    await page.goto("/health-policies/new");
    await expect(page.getByRole("heading", { name: "New Health Policy" })).toBeVisible();

    // `labeledField` for the `<label>`-wrapped fields (see its docstring);
    // Metric/Operator/Value use a real `aria-label` in `ConditionBuilder`
    // instead (not label-wrapped), which doesn't have that problem, so
    // plain `getByLabel` is fine for those.
    await labeledField(page, "Name").fill(name);
    await labeledField(page, "policy_key").fill(policyKey);

    await page.getByLabel("Metric").selectOption("connectivity.fabric_paths_down");
    await page.getByLabel("Operator").selectOption("GTE");
    await page.getByLabel("Value", { exact: true }).fill("0");

    await page.getByPlaceholder("e.g. {down} UCS fabric path is down").fill("test alert");

    const previewResponse = page.waitForResponse((res) =>
      res.url().includes("/api/v1/health-policies/preview"),
    );
    await previewResponse;
    await expect(page.getByText(/server(s)? match/)).toBeVisible();
  }

  test("create two policies sharing a policy_key and see the shadow panel", async ({ page }) => {
    await createPolicy(page, firstName);
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page).toHaveURL(/\/health-policies$/);
    await expect(page.getByRole("link", { name: firstName })).toBeVisible();

    // Second policy, same policy_key: the shadow panel should surface the
    // first policy as a sibling *before* this one is even saved — it's
    // derived from the already-fetched policy list plus the live draft's
    // policy_key, not a save-time check.
    await createPolicy(page, secondName);
    await expect(page.getByTestId("shadow-panel")).toBeVisible();
    await expect(page.getByTestId("shadow-panel")).toContainText(firstName);

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page).toHaveURL(/\/health-policies$/);
    await expect(page.getByRole("link", { name: secondName })).toBeVisible();

    // Both list rows show the shared policy_key.
    const rowA = page.locator("tr", { has: page.getByRole("link", { name: firstName }) });
    const rowB = page.locator("tr", { has: page.getByRole("link", { name: secondName }) });
    await expect(rowA.getByText(policyKey)).toBeVisible();
    await expect(rowB.getByText(policyKey)).toBeVisible();

    // Delete both.
    for (const row of [rowA, rowB]) {
      page.once("dialog", (dialog) => dialog.accept());
      await row.getByRole("button", { name: "Delete" }).click();
    }
    await expect(page.getByRole("link", { name: firstName })).toHaveCount(0);
    await expect(page.getByRole("link", { name: secondName })).toHaveCount(0);
  });
});
