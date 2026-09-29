import { describe, expect, it } from "vitest";
import { age, money, pct, r, signed, tone, utc } from "./format";

describe("format", () => {
  it("shows decimal strings as money and dashes for missing values", () => {
    expect(money("1234.5")).toBe("1,234.50");
    expect(money(null)).toBe("–");
    expect(money("")).toBe("–");
    expect(money("abc")).toBe("–");
  });

  it("signs numbers and R multiples", () => {
    expect(signed("1.5")).toBe("+1.50");
    expect(signed(-2)).toBe("-2.00");
    expect(signed(0)).toBe("0.00");
    expect(r("0.456")).toBe("+0.46R");
    expect(r(undefined)).toBe("–");
    expect(signed("x")).toBe("–");
  });

  it("formats percentages, tones, times and ages", () => {
    expect(pct("2.5")).toBe("2.50%");
    expect(pct(null)).toBe("–");
    expect(tone("1")).toBe("text-emerald-400");
    expect(tone("-1")).toBe("text-rose-400");
    expect(tone("0")).toBe("text-zinc-300");
    expect(utc("2026-09-29T13:45:12Z")).toBe("2026-09-29 13:45");
    expect(utc(null)).toBe("–");
    expect(age(12)).toBe("12s");
    expect(age(150)).toBe("3m");
    expect(age(7200)).toBe("2h");
  });
});
