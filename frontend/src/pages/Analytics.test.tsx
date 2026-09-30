import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CalibrationOut, CommitteeComparison } from "../api/types";
import { CalibrationView, CommitteeCard } from "./Analytics";

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

const BINS = Array.from({ length: 10 }, (_, i) => ({
  lo: i * 10, hi: i === 9 ? 100 : i * 10 + 9, n: i >= 5 ? 20 : 0,
  mean_confidence: i >= 5 ? i * 10 + 5 : null, win_rate: i >= 5 ? (i * 10 - 20) / 100 : null, calibrated: null,
}));
const MODEL = {
  version: 3, source: "analyst", method: "ISOTONIC", status: "ACTIVE", n_samples: 200, brier_before: 0.27,
  brier_after: 0.23, improvement: 0.148, points: [[45, 0.2], [95, 0.68]], created_at: "2026-10-05T01:00:00Z",
  decided_by: "operator", decided_at: "2026-10-05T08:00:00Z",
};
const CAL: CalibrationOut = {
  activation: "approve", min_samples: 150,
  sources: [
    { source: "analyst", n: 100, reliability: BINS, brier_raw: 0.27, active: MODEL,
      candidate: { ...MODEL, version: 4, status: "CANDIDATE", decided_by: null, decided_at: null } },
    { source: "committee", n: 0, reliability: BINS.map((b) => ({ ...b, n: 0 })), brier_raw: null, active: null, candidate: null },
  ],
  models: [{ ...MODEL, version: 4, status: "CANDIDATE", decided_by: null, decided_at: null }, MODEL],
};

describe("calibration view", () => {
  it("draws the reliability and the active map, and offers the candidate", async () => {
    const onAction = vi.fn();
    render(<CalibrationView data={CAL} onAction={onAction} />);
    expect(screen.getByRole("img", { name: "analyst reliability" })).toBeInTheDocument();
    expect(screen.getByTestId("calibration-map")).toBeInTheDocument();
    expect(screen.getByText("No finished trades yet.")).toBeInTheDocument();
    expect(screen.getByText(/Active v3: Brier 0.2700 → 0.2300 \(14.8% better\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith({ version: 4, action: "approve" });
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAction).toHaveBeenLastCalledWith({ version: 4, action: "reject" });
  });
});
