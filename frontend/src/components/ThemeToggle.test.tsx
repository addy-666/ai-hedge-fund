import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { ThemeToggle } from "./ThemeToggle";

afterEach(() => {
  document.documentElement.dataset.theme = "dark";
  localStorage.clear();
});

test("switches between dark and light and remembers the choice", () => {
  document.documentElement.dataset.theme = "dark";
  render(<ThemeToggle />);
  fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(localStorage.getItem("aifund.theme")).toBe("light");
  fireEvent.click(screen.getByRole("button", { name: "Switch to dark theme" }));
  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(localStorage.getItem("aifund.theme")).toBe("dark");
});
