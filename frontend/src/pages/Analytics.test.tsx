import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CommitteeComparison } from "../api/types";
import { CommitteeCard } from "./Analytics";

const DATA: CommitteeComparison = {
  mode: "shadow", bars: 6, first: "2026-10-01T00:00:00Z", last: "2026-10-01T05:00:00Z", days: 0.21,
  arms: [
    { arm: "baseline", trades: 6, total_r: "3.0", mean_r_per_trade: 0.5, win_rate: 0.6667, cost_usd: "0" },
    { arm: "analyst", trades: 5, total_r: "0.5", mean_r_per_trade: 0.1, win_rate: 0.4, cost_usd: "0.006" },
    { arm: "committee", trades: 4, total_r: "3.5", mean_r_per_trade: 0.875, win_rate: 0.75, cost_usd: "0.018" },
  ],
  agreement: 0.8, risk_usd: "49.75",
  vs_analyst: { n: 6, mean_r: 0.4999, ci_low: -0.1, ci_high: 1.2 },
  vs_baseline: { n: 6, mean_r: 0.083, ci_low: null, ci_high: null },
};

describe("committee card", () => {
  it("shows every arm and the uplift with its interval", () => {
    render(<CommitteeCard data={DATA} />);
    expect(screen.getByText(/6 paired bars over 0.2 days/)).toBeInTheDocument();
    expect(screen.getByText("committee")).toBeInTheDocument();
    expect(screen.getByTestId("vs-analyst").textContent).toBe("+0.500R [-0.100, 1.200]");
    expect(screen.getByText(/1R = /)).toBeInTheDocument();
  });

  it("says why it is empty", () => {
    render(<CommitteeCard data={{ ...DATA, bars: 0, mode: "off" }} />);
    expect(screen.getByText("No committee shadow yet (committee.mode is off).")).toBeInTheDocument();
  });
});
