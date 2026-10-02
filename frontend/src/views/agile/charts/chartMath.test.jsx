import { describe, expect, it } from "vitest";
import {
  bandPath, boardDayLabel, boardDayTicks, dateMs, dayTicks, linePath, linearScale, niceTicks, nonWorkingBands,
  stepPath, toMs,
} from "./chartMath";

describe("chart geometry", () => {
  it("maps linearly and survives a flat domain", () => {
    const x = linearScale([0, 10], [100, 200]);
    expect(x(0)).toBe(100);
    expect(x(5)).toBe(150);
    expect(x(10)).toBe(200);
    expect(linearScale([3, 3], [0, 50])(3)).toBe(0);
  });

  it("produces round ticks covering the range", () => {
    expect(niceTicks(0, 16, 4)).toEqual([0, 5, 10, 15, 20]);
    expect(niceTicks(0, 1, 5)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
    expect(niceTicks(0, 0, 4)).toEqual([0, 0.25, 0.5, 0.75, 1]);
    expect(niceTicks(Number.NaN, 5)).toEqual([0]);
    const ticks = niceTicks(0, 37, 5);
    expect(ticks[0]).toBe(0);
    expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(37);
  });

  it("draws step and straight paths", () => {
    const id = (v) => v;
    expect(stepPath([{ x: 0, y: 10 }, { x: 5, y: 4 }, { x: 8, y: 4 }], id, id))
      .toBe("M0.0,10.0 H5.0 V4.0 H8.0 V4.0");
    expect(stepPath([], id, id)).toBe("");
    expect(linePath([{ x: 0, y: 1 }, { x: 2, y: 3 }], id, id)).toBe("M0.0,1.0 L2.0,3.0");
    expect(bandPath([0, 1], [5, 6], [1, 2], id, id)).toBe("M0.0,5.0 L1.0,6.0 L1.0,2.0 L0.0,1.0 Z");
    expect(bandPath([], [], [], id, id)).toBe("");
  });

  it("puts day ticks on local midnights and thins them", () => {
    const start = dateMs("2026-03-02") + 9 * 3_600_000;
    const end = dateMs("2026-03-14");
    const ticks = dayTicks(start, end, 20);
    expect(ticks[0]).toBe(dateMs("2026-03-03"));
    expect(ticks[ticks.length - 1]).toBe(dateMs("2026-03-14"));
    expect(dayTicks(start, end, 4).length).toBeLessThanOrEqual(4);
    expect(dayTicks(end, start)).toEqual([end]);
    expect(toMs("2026-03-02T00:00:00Z")).toBe(Date.parse("2026-03-02T00:00:00Z"));
  });
});

describe("board days", () => {
  // A Kolkata board: its days start at 18:30 UTC the day before.
  const days = [
    { date: "2026-03-06", start: "2026-03-05T18:30:00Z", end: "2026-03-06T18:30:00Z", working: true },
    { date: "2026-03-07", start: "2026-03-06T18:30:00Z", end: "2026-03-07T18:30:00Z", working: false },
    { date: "2026-03-08", start: "2026-03-07T18:30:00Z", end: "2026-03-08T18:30:00Z", working: false },
  ];

  it("puts ticks at the board's midnights, labelled with the board's dates", () => {
    const ticks = boardDayTicks(days, 10);
    expect(ticks.map((t) => t.at)).toEqual(days.map((d) => Date.parse(d.start)));
    expect(ticks.map((t) => t.label)).toEqual(["2026-03-06", "2026-03-07", "2026-03-08"].map(boardDayLabel));
    expect(boardDayTicks(days, 2).length).toBeLessThanOrEqual(2);
    expect(boardDayTicks(undefined)).toEqual([]);
  });

  it("keeps ticks and bands inside the plotted range", () => {
    // Started Friday 17:00 UTC, plotted until Sunday noon UTC.
    const domain = [Date.parse("2026-03-06T17:00:00Z"), Date.parse("2026-03-08T12:00:00Z")];
    expect(boardDayTicks(days, 10, domain).map((t) => t.label)).toEqual(["2026-03-07", "2026-03-08"].map(boardDayLabel));
    expect(nonWorkingBands(days, domain)).toEqual([
      { from: Date.parse("2026-03-06T18:30:00Z"), to: Date.parse("2026-03-07T18:30:00Z") },
      { from: Date.parse("2026-03-07T18:30:00Z"), to: domain[1] },
    ]);
    expect(nonWorkingBands(days, [0, Date.parse("2026-03-06T00:00:00Z")])).toEqual([]);
  });

  it("labels a date the same in every timezone", () => {
    // Noon UTC formatted in UTC: never the day before or after.
    expect(boardDayLabel("2026-03-07")).toBe(
      new Date(Date.UTC(2026, 2, 7)).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }),
    );
  });

  it("shades exactly the non-working days' instants", () => {
    expect(nonWorkingBands(days)).toEqual([
      { from: Date.parse("2026-03-06T18:30:00Z"), to: Date.parse("2026-03-07T18:30:00Z") },
      { from: Date.parse("2026-03-07T18:30:00Z"), to: Date.parse("2026-03-08T18:30:00Z") },
    ]);
    expect(nonWorkingBands(null)).toEqual([]);
  });
});

describe("edge ranges", () => {
  it("gives a range inside one day a tick", () => {
    const start = dateMs("2026-03-02") + 9 * 3_600_000;
    expect(dayTicks(start, start + 6 * 3_600_000)).toEqual([start]);
  });

  it("covers negative and tiny domains with finite, increasing ticks", () => {
    for (const [lo, hi] of [[-5, 5], [0, 0.001], [0, 0], [3, 3]]) {
      const ticks = niceTicks(lo, hi, 5);
      expect(ticks.length).toBeGreaterThan(0);
      expect(ticks.every(Number.isFinite)).toBe(true);
      expect(ticks).toEqual([...ticks].sort((a, b) => a - b));
      expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(hi);
    }
  });
});
