import { expect, test as setup } from "@playwright/test";

// One login for the whole run (the API allows 5 login attempts a minute per address); the other tests reuse
// the session cookie.
export const STATE = "e2e/.auth/state.json";

setup("log in", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Password").fill("demo password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("navigation").first()).toBeVisible();
  await page.context().storageState({ path: STATE });
});
