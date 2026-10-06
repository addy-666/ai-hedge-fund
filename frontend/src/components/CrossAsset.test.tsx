import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { CrossAssetOut } from "../api/types";
import { CrossAssetView } from "./CrossAsset";

const DATA: CrossAssetOut = {
  enabled: true, timeframe: "H1", max_age_minutes: 120, instruments: ["EURUSD", "XAUUSD", "BTCUSD"],
  references: ["EURUSD"],
  rows: [{
    symbol: "XAUUSD", bar_time: "2026-10-06T09:45:00Z",
    cells: [
      { instrument: "EURUSD", open: true, corr100: 0.38, ret24_z: 1.4, ema_stack: "BULL" },
      { instrument: "BTCUSD", open: false, corr100: null, ret24_z: null, ema_stack: null },
    ],
  }],
};

describe("cross-asset card (roadmap 10.6)", () => {
  it("shows what the newest decision saw of each other market", () => {
    render(<CrossAssetView data={DATA} />);
    expect(screen.getByText(/3 instruments on H1 bars \(data only: EURUSD\)/)).toBeInTheDocument();
    expect(screen.getByText("+0.38")).toBeInTheDocument();
    expect(screen.getByText("+1.4σ")).toBeInTheDocument();
    expect(screen.getByText("shut")).toBeInTheDocument(); // BTCUSD: no fresh bars
    expect(screen.getByText("self")).toBeInTheDocument(); // XAUUSD never sees itself
  });

  it("says when it is off or has nothing yet", () => {
    const { rerender } = render(<CrossAssetView data={{ ...DATA, enabled: false }} />);
    expect(screen.getByText(/Cross-asset features are off/)).toBeInTheDocument();
    rerender(<CrossAssetView data={{ ...DATA, rows: [] }} />);
    expect(screen.getByText("No decision with cross-asset features yet.")).toBeInTheDocument();
  });
});
