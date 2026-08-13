import type { APIRequestContext, Locator, Page } from "@playwright/test";

/**
 * Locates a form control by its wrapping `<label>`'s *own* text — not
 * `page.getByLabel()` and not `page.locator("label", { hasText })`: both
 * of those match against the label's full accessible-name/textContent,
 * which for this codebase's `<label>Text<select>…option…</select></label>`
 * pattern includes every `<option>`'s text too (a real, confirmed
 * Chromium behavior, not a Playwright quirk — e.g. the "Source" field's
 * computed name is "SourceSITE_CUSTOMMANAGER_CUSTOMVENDOR_CUSTOM…", which
 * contains "Vendor" as a substring and collides with the real Vendor
 * field). XPath's `text()` axis selects only the label's own direct child
 * text node, excluding the nested control's descendant text entirely, so
 * it doesn't have this problem.
 */
export function labeledField(page: Page, label: string): Locator {
  return page
    .locator(`xpath=//label[normalize-space(text())="${label}"]`)
    .locator("select, input, textarea");
}

/**
 * A per-run prefix so test-created resources never collide across
 * repeated local runs (no cleanup-between-runs dependency) and are
 * trivially greppable in the admin UI/DB if a run is ever aborted before
 * its own cleanup runs. Not used for anything the UI itself asserts on
 * semantically — just uniqueness.
 */
export function uniqueName(label: string): string {
  return `e2e-${label}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/**
 * Best-effort cleanup for a classification rule created by name, used in
 * `test.afterEach` so a failed assertion mid-test (which skips whatever
 * cleanup the test body itself would have done) doesn't leave test data
 * behind for the next run. Swallows "not found" — the test's own cleanup
 * path may have already deleted it.
 */
export async function deleteClassificationRuleByName(
  request: APIRequestContext,
  name: string,
): Promise<void> {
  // No server-side name filter on this endpoint (the rule collection is
  // small — dozens, not thousands — so `list_all()` is the real contract,
  // see `app.infrastructure.mongodb.classification_rule_repository`);
  // filter client-side.
  const response = await request.get("/api/v1/classification-rules");
  if (!response.ok()) return;
  const body = (await response.json()) as { items: { id: string; name: string }[] };
  for (const item of body.items.filter((r) => r.name === name)) {
    await request.delete(`/api/v1/classification-rules/${item.id}`);
  }
}

export async function deleteHealthPolicyByName(
  request: APIRequestContext,
  name: string,
): Promise<void> {
  const response = await request.get("/api/v1/health-policies");
  if (!response.ok()) return;
  const body = (await response.json()) as { items: { id: string; name: string }[] };
  for (const item of body.items.filter((p) => p.name === name)) {
    await request.delete(`/api/v1/health-policies/${item.id}`);
  }
}
