import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel("Password").fill("demo password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("navigation")).toBeVisible();
}

test("a wrong password is refused", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Password").fill("not the password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
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
    await page.getByRole("navigation").getByRole("link", { name: link, exact: true }).click();
    await expect(page.getByText(text).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("the trade dossier shows the chart and the decision", async ({ page }) => {
  await login(page);
  await page.getByRole("navigation").getByRole("link", { name: "Journal", exact: true }).click();
  await page.getByRole("row", { name: /XAUUSD/ }).first().click();
  await expect(page.getByText("Decision", { exact: true })).toBeVisible();
  await expect(page.locator("canvas").first()).toBeVisible();
});

test("pause asks once and is queued for the engine", async ({ page }) => {
  await login(page);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await page.getByRole("button", { name: "Yes, pause" }).click();
  await expect(page.getByRole("status").filter({ hasText: "PAUSE sent" })).toBeVisible();
});
