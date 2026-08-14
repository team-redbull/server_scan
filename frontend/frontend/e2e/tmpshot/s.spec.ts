import { test } from "@playwright/test";
const OUT = "/tmp/claude-1000/-home-tomer-code-server-scan/88537d77-8f51-4069-84cc-0bb875679e95/scratchpad";
test("shots", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await page.waitForSelector("text=Site One");
  await page.screenshot({ path: `${OUT}/01-sites-light.png` });
  await page.goto("/servers");
  await page.waitForSelector("tbody tr");
  await page.screenshot({ path: `${OUT}/02-servers-light.png` });
});
test("shots dark", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await page.waitForSelector("text=Site One");
  await page.screenshot({ path: `${OUT}/03-sites-dark.png` });
  await page.goto("/servers");
  await page.waitForSelector("tbody tr");
  await page.screenshot({ path: `${OUT}/04-servers-dark.png` });
});
