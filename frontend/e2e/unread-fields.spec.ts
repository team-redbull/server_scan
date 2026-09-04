import { expect, test } from "@playwright/test";

/**
 * The regression 1a896af caught the hard way: a field the collector could
 * not read arrives as JSON `null`, and a guard written for `undefined`
 * let `null.toFixed()` through, which unmounted the whole detail page.
 * The E2E suite reported it as "the Hardware tab button does not exist".
 *
 * The other specs only ever open the first row of the default sort, which
 * is a fully-populated server — so this one picks its subject from the
 * live API instead: the server with the most `unread_fields`, i.e. the
 * one whose response carries the most nulls. Hand-written unit fixtures
 * cannot cover this, because they are written from the same mental model
 * of the payload that was wrong in the first place.
 */
test.describe("a server the collector could only read part of", () => {
  test("renders every tab without unmounting the page", async ({ page, request }) => {
    const list = await (await request.get("/api/v1/servers?page_size=200")).json();
    let subject: { id: string; name: string } | null = null;
    for (const row of list.items) {
      const detail = await (await request.get(`/api/v1/servers/${row.id}`)).json();
      if (detail.unread_fields.length > 0) {
        subject = { id: row.id, name: detail.name };
        break;
      }
    }
    expect(subject, "the seeded fleet has no partially-read server").not.toBeNull();

    // A React render that throws unmounts silently as far as the DOM is
    // concerned; the only durable evidence is on the console.
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    await page.goto(`/servers/${subject?.id ?? ""}`);
    await expect(page.getByRole("heading", { name: subject?.name ?? "", exact: true })).toBeVisible();

    for (const tab of ["Overview", "Hardware", "Network", "Connectivity"]) {
      await page.getByRole("button", { name: tab, exact: true }).click();
      await expect(page.getByRole("button", { name: tab, exact: true })).toHaveAttribute(
        "aria-current",
        "page",
      );
    }

    // Nothing unreadable may reach the page as a NaN or a literal "null" —
    // the tabs degrade to the dash or to "Not reported".
    const body = await page.locator("main").innerText();
    expect(body).not.toContain("NaN");
    expect(body).not.toContain("null");
    expect(body).not.toContain("undefined");
    expect(errors).toEqual([]);
  });
});
