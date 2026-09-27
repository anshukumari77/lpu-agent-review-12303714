import { expect, test } from "@playwright/test";
import { testView } from "../src/test-fixtures";

// Offline browser fixtures exercise the REAL application, never /preview.
// They do not qualify live voice, recordings or delivery of invitations.
test("operator puts workflow creation before history and keeps separate invitations unconsumed", async ({
  page,
}, testInfo) => {
  const view = {
    ...testView({
      id: "guidance-browser",
      title: "Synthetic UI verification: invoice approvals",
      offer: "sponsored",
    }),
    role: "operator",
  };
  const writes: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url()).pathname;
    if (request.method() === "POST") writes.push(url);
    const json =
      url === "/api/me"
        ? { role: "operator" }
        : url === "/api/health"
          ? {
              status: "ok",
              readiness: { ready: true, configured: {} },
              version: "browser-fixture",
            }
          : url === "/api/packs"
            ? { packs: [{ id: "general", title: "General workflow" }] }
            : url === "/api/sessions" && request.method() === "POST"
              ? {
                  session: view.session,
                  invitations: {
                    client:
                      "http://localhost/review/guidance-browser#invite=synthetic-client",
                    facilitator:
                      "http://localhost/review/guidance-browser#invite=synthetic-facilitator",
                  },
                }
              : url === "/api/sessions/guidance-browser"
                ? view
                : {
                    sessions: writes.includes("/api/sessions")
                      ? [view.session]
                      : [],
                  };
    await route.fulfill({ json });
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Create a real review session" }),
  ).toBeVisible();
  expect(
    await page
      .locator(".create-panel")
      .evaluate(
        (panel) =>
          !!(
            panel.compareDocumentPosition(
              document.querySelector(".session-ledger")!,
            ) & Node.DOCUMENT_POSITION_FOLLOWING
          ),
      ),
  ).toBe(true);
  expect(
    await page
      .locator(".create-panel")
      .evaluate(
        (panel) =>
          !!(
            panel.compareDocumentPosition(
              document.querySelector(".inference-panel")!,
            ) & Node.DOCUMENT_POSITION_FOLLOWING
          ),
      ),
  ).toBe(true);
  const title = page.getByRole("textbox", {
    name: "Which workflow will you review?",
  });
  await title.fill(view.session.title);
  await page.getByRole("radio", { name: /Sponsored/ }).check();
  await page
    .getByRole("button", { name: "Create session & invitations" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Send the two invitations separately." }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Private invitations" }),
  ).toBeFocused();
  await page.screenshot({
    path: testInfo.outputPath("invitations-viewport.png"),
  });
  await expect(page.locator('a[href*="#invite="]')).toHaveCount(0);
  expect(writes).toEqual(["/api/sessions"]);
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    await title.evaluate((el) => parseFloat(getComputedStyle(el).fontSize)),
  ).toBeGreaterThanOrEqual(16);
  await page
    .getByRole("heading", { name: "Create a real review session" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("operator-guidance.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("real room keeps the next step ahead of the stage and explains recording blockers on small screens", async ({
  page,
}, testInfo) => {
  let view = testView({
    title: "Synthetic UI verification: invoice approvals",
    client_consent: true,
  });
  const writes: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url()).pathname;
    if (request.method() === "POST") writes.push(url);
    await route.fulfill({
      json:
        url === "/api/me"
          ? { role: "client", session_id: "review-one" }
          : url === "/api/health"
            ? {
                status: "ok",
                readiness: { ready: true, configured: {} },
                version: "browser-fixture",
              }
            : url.endsWith("/events")
              ? { events: [] }
              : view,
    });
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/review/review-one");
  const next = page.getByRole("region", { name: "Your next step" });
  await expect(
    next.getByRole("heading", { name: "Wait for your facilitator" }),
  ).toBeVisible();
  const box = (await next.boundingBox())!;
  expect(box.y).toBeLessThan(
    (await page.locator(".phase-strip").boundingBox())!.y,
  );
  expect(box.y + box.height).toBeLessThan(
    (await page.locator(".control-dock").boundingBox())!.y,
  );
  expect(
    await next
      .getByRole("button")
      .evaluate((el) => el.getBoundingClientRect().height),
  ).toBeGreaterThanOrEqual(44);
  expect(
    await next
      .locator("h2")
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize)),
  ).toBeGreaterThanOrEqual(24);
  view = testView({
    title: view.session.title,
    client_consent: true,
    facilitator_consent: true,
    status: "introduction",
    recording_status: "failed",
    started_at: new Date().toISOString(),
  });
  await next.getByRole("button", { name: "Check session status" }).click();
  await expect(
    next.getByRole("heading", { name: "Ask your operator to fix recording" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Turn microphone on" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Join the call" })).toHaveCount(
    0,
  );
  expect(writes).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const blocked = (await next.boundingBox())!;
  expect(blocked.y + blocked.height).toBeLessThan(
    (await page.locator(".control-dock").boundingBox())!.y,
  );
  await page.screenshot({ path: testInfo.outputPath("room-viewport.png") });
  await page.screenshot({
    path: testInfo.outputPath("room-guidance.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
