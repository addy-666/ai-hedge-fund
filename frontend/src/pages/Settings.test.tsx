import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithClient, stubFetch } from "../test/helpers";
import { Settings } from "./Settings";

const CONFIG = { yaml: "engine:\n  mode: SIM\n", sha256: "abc", version_id: null, json_schema: {} };

afterEach(() => vi.unstubAllGlobals());

async function edit(text: string) {
  const box = await screen.findByRole("textbox", { name: "config" });
  await userEvent.clear(box);
  await userEvent.type(box, text);
  await userEvent.click(screen.getByRole("button", { name: "Validate & save" }));
}

describe("Settings", () => {
  it("shows field errors from the server", async () => {
    stubFetch({
      "GET /api/config": { body: CONFIG },
      "GET /api/config/versions": { body: [] },
      "PUT /api/config": { status: 422, body: [{ field: "risk.limits", message: "bad value" }] },
    });
    renderWithClient(<Settings />);
    await edit("risk: 1");
    expect(await screen.findByRole("alert")).toHaveTextContent("risk.limits: bad value");
  });

  it("asks for the password on a riskier change, then saves", async () => {
    let puts = 0;
    const calls = stubFetch({
      "GET /api/config": { body: CONFIG },
      "GET /api/config/versions": { body: [] },
      "PUT /api/config": () =>
        ++puts === 1
          ? { status: 403, body: { detail: "reauth_required: this change raises risk" } }
          : { body: { version_id: "v2", changed: true, command_id: "c9" } },
      "POST /api/auth/reauth": { body: { ok: true } },
    });
    renderWithClient(<Settings />);
    await edit("risk: 2");

    await userEvent.type(await screen.findByLabelText("password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByRole("status")).toHaveTextContent("saved as version v2");
    expect(calls.filter((c) => c.method === "PUT")).toHaveLength(2);
    expect(calls.find((c) => c.path === "/api/auth/reauth")?.body).toEqual({ password: "correct horse battery" });
  });
});
