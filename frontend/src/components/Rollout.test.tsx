import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RolloutOut } from "../api/types";
import { RolloutView } from "./Rollout";

const READY: RolloutOut = {
  level: "L2", level_name: "DEMO", next_level: "L3", period_start: "2026-09-28T09:00:00Z", ready: true,
  gates: [
    { name: "closed_trades", status: "PASS", value: "104", need: "≥ 100" },
    { name: "kill_switch_tested", status: "PASS", value: "1 FLATTEN_ALL done", need: "≥ 1" },
  ],
  signoffs: [],
};

describe("rollout gates", () => {
  it("signs off a ready level with a note", async () => {
    const onSignoff = vi.fn();
    render(<RolloutView data={READY} onSignoff={onSignoff} />);
    expect(screen.getByText("closed trades")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Sign off L2 → L3" });
    expect(button).toBeDisabled(); // a note first
    await userEvent.type(screen.getByLabelText("sign-off note"), "4 weeks, 104 trades, all green");
    await userEvent.click(button);
    expect(onSignoff).toHaveBeenCalledWith("4 weeks, 104 trades, all green");
  });

  it("will not sign off while a gate fails", () => {
    const failing = { ...READY, ready: false, gates: [{ name: "duplicate_orders", status: "FAIL", value: "1", need: "0" }] };
    render(<RolloutView data={failing} onSignoff={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Sign off L2 → L3" })).toBeDisabled();
    expect(screen.getByLabelText("sign-off note")).toBeDisabled();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
  });
});
