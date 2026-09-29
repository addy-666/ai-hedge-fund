import { describe, expect, it } from "vitest";
import { confidenceMath } from "./DecisionTrace";

describe("confidenceMath", () => {
  it("shows raw -> calibrated -> penalty = final", () => {
    expect(
      confidenceMath({ llm_confidence: 72, calibrated_confidence: 65, penalty_points: 5, final_confidence: 60 }),
    ).toBe("raw 72 → calibrated 65 → − 5 rule penalty = 60");
  });

  it("omits a zero penalty and dashes missing stages", () => {
    expect(confidenceMath({ llm_confidence: null, calibrated_confidence: null, penalty_points: 0, final_confidence: 55 })).toBe(
      "raw – → calibrated – = 55",
    );
    expect(confidenceMath({ llm_confidence: 70, calibrated_confidence: 70, penalty_points: 0, final_confidence: null })).toBe("–");
  });
});
