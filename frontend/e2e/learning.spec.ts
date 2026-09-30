import { expect, test, type Page } from "@playwright/test";

// Learning Lab (roadmap 7.9) against the demo API, whose command worker runs rule commands through the real
// learning loop: approving the shadow block rule (12 losing live matches) makes it ACTIVE in a new rulebook.
async function openLab(page: Page, tab = "rules") {
  await page.goto("/"); // the session from e2e/auth.setup.ts
  await page.getByRole("navigation").first().getByRole("link", { name: "Learning Lab" }).click();
  if (tab !== "rules") await page.getByRole("navigation", { name: "learning lab" }).getByRole("button", { name: tab }).click();
}

test("approving a shadow rule makes it active in a new rulebook version", async ({ page }) => {
  await openLab(page);
  const active = page.getByLabel("ACTIVE rules");
  await expect(active.getByText("R-0001 v1")).toBeVisible();
  await page.getByLabel("SHADOW rules").getByText("R-0002 v1").click();
  await expect(page.getByText("needs approval").first()).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await page.getByRole("button", { name: "Yes, approve" }).click();
  const password = page.getByLabel("password", { exact: true });
  if (await password.isVisible().catch(() => false)) {
    await password.fill("demo password");
    await page.getByRole("button", { name: "Confirm" }).click();
  }
  await expect(page.getByRole("status").filter({ hasText: "approve sent to the engine" })).toBeVisible();
  await expect.poll(async () => {
    await page.reload();
    await page.getByLabel("ACTIVE rules").getByText("R-0001 v1").waitFor(); // the board has loaded
    return page.getByLabel("ACTIVE rules").getByText("R-0002 v1").count();
  }, { timeout: 15_000 }).toBe(1);
  await page.getByRole("navigation", { name: "learning lab" }).getByRole("button", { name: "Rulebook" }).click();
  await page.getByRole("row", { name: /^v3/ }).click();
  await expect(page.getByText("activated", { exact: true }).locator("..")).toContainText("R-0002v1");
});

test("an operator rule is created as a candidate", async ({ page }) => {
  await openLab(page, "New rule");
  await page.getByLabel("direction").selectOption("LONG");
  await page.getByLabel("feature 1").fill("h1.rsi14");
  await page.getByLabel("op 1").selectOption(">");
  await page.getByLabel("value 1").fill("75");
  await page.getByLabel("hypothesis").fill("Exhausted longs revert to the mean.");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page.getByRole("status")).toContainText("created as CANDIDATE");
  await page.getByLabel("value 1").fill("250");  // outside the RSI range: the API refuses it
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page.getByRole("alert")).toContainText("outside");
});

test("the audit report and its findings", async ({ page }) => {
  await openLab(page, "Audit runs");
  await page.getByRole("row", { name: /nightly/ }).click();
  await expect(page.getByText("Long entries above H1 RSI 70 lose.")).toBeVisible();
});
