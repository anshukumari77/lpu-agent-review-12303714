import { test, expect } from "@playwright/test";

for (const reducedMotion of ["reduce", "no-preference"] as const) {
  test(`skip link stays offscreen until keyboard focus (${reducedMotion})`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion });
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Operator sign in" }),
    ).toBeVisible();
    const skip = page.locator(".skip-link");
    expect(
      await skip.evaluate((e) => e.getBoundingClientRect().bottom),
    ).toBeLessThanOrEqual(0);
    await page.keyboard.press("Tab");
    await expect(skip).toBeFocused();
    expect((await skip.boundingBox())!.y).toBeGreaterThanOrEqual(0);
    await page.keyboard.press("Tab");
    await expect(skip).not.toBeFocused();
    expect(
      await skip.evaluate((e) => e.getBoundingClientRect().bottom),
    ).toBeLessThanOrEqual(0);
  });
}

test("real health explains missing prerequisites without raw config names", async ({
  page,
  request,
}, testInfo) => {
  const response = await request.get("/api/health");
  expect(response.ok()).toBeTruthy();
  const health = await response.json();
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Operator sign in" }),
  ).toBeVisible();
  if (health.readiness.configured.openai_api_key === false) {
    await expect(page.locator(".readiness-bar summary")).toContainText(
      "OpenAI API key (native voice)",
    );
    await expect(page.locator(".readiness-bar summary")).not.toContainText(
      "openai_api_key",
    );
  }
  await page.screenshot({
    path: testInfo.outputPath("readiness-viewport.png"),
    fullPage: false,
  });
});
