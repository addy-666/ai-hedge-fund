import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/"); // the session from e2e/auth.setup.ts
  await expect(page.getByRole("navigation").first()).toBeVisible();
}

test.describe("without a session", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a wrong password is refused", async ({ page }) => {
  await page.goto("/");
    await page.getByLabel("Password").fill("not the password");
    await page.getByRole("button", { name: "Log in" }).click();
    await expect(page.getByRole("alert")).toBeVisible();
  });
});

test("every page renders on the seeded database without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await login(page);
  await expect(page.getByText("Equity (30 days)")).toBeVisible();

  const pages: [string, string | RegExp][] = [
    ["Positions", "Open positions (1)"],
    ["Decisions", "XAUUSD"],
    ["Journal", "Closed trades"],
    ["Analytics", /trades/i],
    ["Agents & LLM", /LLM/],
    ["Settings", "Trading config"],
    ["System", /heartbeat/i],
  ];
  for (const [link, text] of pages) {
    await page.getByRole("navigation").first().getByRole("link", { name: link, exact: true }).click();
    await expect(page.getByText(text).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("the trade dossier shows the chart and the decision", async ({ page }) => {
  await login(page);
  await page.getByRole("navigation").first().getByRole("link", { name: "Journal", exact: true }).click();
  await page.getByRole("row", { name: /XAUUSD/ }).first().click();
  await expect(page.getByText("Decision", { exact: true })).toBeVisible();
  await expect(page.getByText("The pullback held the EMA50 zone; the target was realistic.")).toBeVisible();
  await expect(page.locator("canvas").first()).toBeVisible();
});

test("pause asks once and is queued for the engine", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await page.getByRole("button", { name: "Yes, pause" }).click();
  await expect(page.getByRole("status").filter({ hasText: "PAUSE sent" })).toBeVisible();
});

test("analytics shows the committee shadow and approving a calibration is queued (with re-auth if stale)", async ({ page }) => {
  await login(page);
  await page.getByRole("navigation").first().getByRole("link", { name: "Analytics", exact: true }).click();
  await expect(page.getByText(/6 paired bars over/)).toBeVisible();
  await expect(page.getByRole("img", { name: "analyst reliability" })).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  const sent = page.getByRole("status").filter({ hasText: "approve v2 sent" });
  const password = page.getByLabel("password");
  await expect(sent.or(password)).toBeVisible(); // a login older than 5 minutes needs the password again
  if (await password.isVisible()) {
    await password.fill("demo password");
    await page.getByRole("button", { name: "Confirm" }).click();
  }
  await expect(sent).toBeVisible();
});
