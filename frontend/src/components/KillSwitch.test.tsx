import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setCsrf } from "../api/client";
import { renderWithClient, stubFetch } from "../test/helpers";
import { KillSwitch } from "./KillSwitch";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf(null);
});

describe("KillSwitch", () => {
  it("flattens only after one confirmation, with the CSRF header and no password", async () => {
    setCsrf("csrf-1");
    const calls = stubFetch({ "POST /api/engine/commands": { status: 202, body: { command_id: "c1" } } });
    renderWithClient(<KillSwitch />);

    await userEvent.click(screen.getByRole("button", { name: "Flatten all" }));
    expect(screen.getByRole("dialog", { name: "Flatten all positions?" })).toBeInTheDocument();
    expect(calls).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Yes, flatten all" }));
    expect(await screen.findByRole("status")).toHaveTextContent("FLATTEN_ALL sent");
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({ type: "FLATTEN_ALL" });
    expect(post?.headers["X-CSRF-Token"]).toBe("csrf-1");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sends nothing when the confirmation is cancelled", async () => {
    const calls = stubFetch({});
    renderWithClient(<KillSwitch />);
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });
});
