import { expect, test } from "@playwright/test";

const stages = [
  "welcome",
  "permissions",
  "setup",
  "introduction",
  "review",
  "recap",
  "report",
];

test("the explicit design journey never calls accounts, providers or capture APIs", async ({
  page,
}) => {
  const apiRequests: string[] = [];
  const errors: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/"))
      apiRequests.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    let calls = 0;
    Object.defineProperty(window, "__journeyCaptureCalls", {
      get: () => calls,
    });
    if (navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia = async () => {
        calls++;
        throw new Error("Unexpected capture");
      };
      navigator.mediaDevices.getDisplayMedia = async () => {
        calls++;
        throw new Error("Unexpected screen");
      };
    }
  });
  await page.goto("/preview");
  await expect(
    page.getByRole("button", { name: "Prepare for my review" }),
  ).toBeVisible();
  for (const stage of stages) {
    await page.locator(`[data-preview-step="${stage}"]`).click();
    await expect(page.locator("main")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
  expect(apiRequests).toEqual([]);
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(() => Reflect.get(window, "__journeyCaptureCalls")),
  ).toBe(0);
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test("welcome leads to separate unchecked permissions without pretending to be live", async ({
  page,
}) => {
  await page.goto("/preview");
  await page.getByRole("button", { name: "Prepare for my review" }).click();
  const checks = page.locator('.consent-form input[type="checkbox"]');
  await expect(checks).toHaveCount(2);
  await expect(checks.nth(0)).not.toBeChecked();
  await expect(checks.nth(1)).not.toBeChecked();
  const confirm = page.getByRole("button", { name: "Confirm my consent" });
  await expect(confirm).toBeDisabled();
  await checks.nth(0).check();
  await expect(confirm).toBeDisabled();
  await checks.nth(1).check();
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.locator(".consent-form")).toHaveCount(0);
  await expect(page.locator("main")).toBeVisible();
});
