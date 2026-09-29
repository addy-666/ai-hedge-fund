import { describe, expect, it } from "vitest";
import { invalidationsFor } from "./socket";

describe("invalidationsFor", () => {
  it("maps event types to the queries they make stale", () => {
    expect(invalidationsFor("engine.state")).toEqual([["system"]]);
    expect(invalidationsFor("trade.closed")).toEqual([["positions"], ["trades"], ["account"]]);
    expect(invalidationsFor("decision.made")).toEqual([["decisions"]]);
    expect(invalidationsFor("intent.filled")).toEqual([["decisions"]]);
    expect(invalidationsFor("risk.limit_breach")).toEqual([["account"], ["system"]]);
    expect(invalidationsFor("something.else")).toEqual([]);
  });
});
