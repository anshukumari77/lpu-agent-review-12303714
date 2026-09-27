import { test, expect } from "@playwright/test";
/** Real API only. No page.route, response fixtures, or production demo bypass. */
test("entry renders a truthful access state without requesting devices", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    let calls = 0;
    const devices = navigator.mediaDevices;
    if (devices) {
      devices.getUserMedia = async () => {
        calls++;
        throw new Error("Unexpected capture in entry test");
      };
      if (devices.getDisplayMedia)
        devices.getDisplayMedia = async () => {
          calls++;
          throw new Error("Unexpected capture in entry test");
        };
    }
    Object.defineProperty(window, "__captureCalls", { get: () => calls });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", {
      name: /Operator sign in|Access could not be checked\./,
    }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(await page.evaluate(() => Reflect.get(window, "__captureCalls"))).toBe(
    0,
  );
  expect(errors).toEqual([]);
  const inputs = page.locator("input:visible");
  for (const input of await inputs.all())
    expect(
      await input.evaluate((element) =>
        parseFloat(getComputedStyle(element).fontSize),
      ),
    ).toBeGreaterThanOrEqual(16);
});
test("private review ID alone never opens a report or microphone", async ({
  page,
  request,
}) => {
  const health = await request.get("/api/health").catch(() => null);
  test.skip(
    !health?.ok(),
    "Start the real API on 8094 for authenticated scope integration.",
  );
  await page.goto("/review/not-an-authorised-review");
  await expect(
    page.getByRole("heading", { name: "A private invitation is needed." }),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: "Internal draft" })).toHaveCount(
    0,
  );
});
test("operator creates a real sponsored session and client enters with unchecked consent", async ({
  page,
  browser,
  request,
  baseURL,
}) => {
  const token = process.env.BEEP_E2E_OPERATOR_TOKEN;
  test.skip(
    !token,
    "Set BEEP_E2E_OPERATOR_TOKEN for the authorised local test workspace.",
  );
  const health = await request.get("/api/health");
  expect(health.ok()).toBeTruthy();
  await page.goto("/");
  await page.getByLabel("Operator password").fill(token!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Workflow reviews" }),
  ).toBeVisible();
  await page
    .getByLabel("Which workflow will you review?")
    .fill("Synthetic browser acceptance review");
  await page.getByLabel("Sponsored · subject to workspace limits").check();
  await page
    .getByRole("button", { name: "Create session & invitations" })
    .click();
  await expect(
    page.getByText("Send the two invitations separately."),
  ).toBeVisible();
  const invitation = await page
    .getByLabel("client invitation URL")
    .inputValue();
  // A fresh browser context prevents invitation exchange replacing the operator cookie.
  const client = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const clientPage = await client.newPage();
  try {
    const url = new URL(invitation);
    const origin = new URL(baseURL!);
    url.protocol = origin.protocol;
    url.host = origin.host;
    await clientPage.goto(url.toString());
    await expect(
      clientPage.getByRole("button", { name: "Review my permissions" }),
    ).toBeVisible();
    await clientPage
      .getByRole("button", { name: "Review my permissions" })
      .click();
    await expect(
      clientPage.getByRole("heading", { name: "A review you control." }),
    ).toBeVisible();
    expect(new URL(clientPage.url()).hash).toBe("");
    for (const checkbox of await clientPage.getByRole("checkbox").all())
      await expect(checkbox).not.toBeChecked();
    await expect(
      clientPage.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    expect(await clientPage.evaluate(() => localStorage.length)).toBe(0);
    expect(
      await clientPage.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const dock = clientPage.getByLabel("Review capture controls");
    await expect(dock).toBeVisible();
    for (const button of await dock.getByRole("button").all()) {
      const box = await button.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.width).toBeGreaterThanOrEqual(44);
    }
  } finally {
    await client.close();
  }
});
