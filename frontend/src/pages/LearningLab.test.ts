import { describe, expect, it } from "vitest";
import { evidenceLine, parseValue } from "./LearningLab";

describe("rule editor values", () => {
  it("turns text into the DSL's numbers, flags, names and lists", () => {
    expect(parseValue(">", "70")).toBe(70);
    expect(parseValue("==", "true")).toBe(true);
    expect(parseValue("==", "NY")).toBe("NY");
    expect(parseValue("between", "30, 45.5")).toEqual([30, 45.5]);
    expect(parseValue("in", "NY,LONDON")).toEqual(["NY", "LONDON"]);
    expect(parseValue("==", "")).toBe("");
  });
});

describe("evidence line", () => {
  it("summarises the validator's numbers", () => {
    expect(evidenceLine({ n_matched: 34, mean_matched: -0.52, mean_unmatched: 0.14, n_holdout: 11 })).toBe(
      "n=34 · -0.52R vs +0.14R · holdout n=11",
    );
    expect(evidenceLine(null)).toBe("not validated yet");
    expect(evidenceLine({ n_matched: 3 })).toBe("n=3 · – vs – · holdout n=–");
  });
});
