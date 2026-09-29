import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithClient, stubFetch } from "../test/helpers";
import { EngineControls } from "./EngineControls";

afterEach(() => vi.unstubAllGlobals());

describe("EngineControls", () => {
  it("asks for the password when the API requires it, then shows the engine's result", async () => {
    let posts = 0;
    const calls = stubFetch({
      "POST /api/engine/commands": () =>
        ++posts === 1
          ? { status: 403, body: { detail: "reauth_required: REARM after a drawdown halt" } }
          : { status: 202, body: { command_id: "c7" } },
      "POST /api/auth/reauth": { body: { ok: true } },
      "GET /api/commands/c7": { body: { id: "c7", type: "REARM", status: "DONE", result: { state: "PAUSED" } } },
    });
    renderWithClient(<EngineControls />);
    await userEvent.click(screen.getByRole("button", { name: "Rearm" }));
    await userEvent.click(screen.getByRole("button", { name: "Yes, rearm" }));
    await userEvent.type(await screen.findByLabelText("password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("status")).toHaveTextContent('REARM: DONE · {"state":"PAUSED"}');
    expect(calls.filter((c) => c.path === "/api/engine/commands").map((c) => c.body)).toEqual([{ type: "REARM" }, { type: "REARM" }]);
  });
});
