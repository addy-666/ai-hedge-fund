// lightweight-charts wrappers: the equity curve and a candlestick chart with trade markers and level lines.
import {
  AreaSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode, LineStyle, type IChartApi,
  type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useSyncExternalStore } from "react";
import type { BarOut, EquityPoint } from "../api/types";
import { cssVar, theme } from "../lib/theme";

// The palette comes from the --chart-* variables in index.css, read when a chart is built; a theme switch
// rebuilds the chart (rare, and cheaper than re-styling every series).
type Palette = Record<"text" | "grid" | "border" | "line" | "fill" | "fillEnd" | "up" | "down" | "amber" | "crosshair" | "label" | "bg", string>;
const palette = (): Palette => ({
  text: cssVar("--chart-text"), grid: cssVar("--chart-grid"), border: cssVar("--chart-border"), line: cssVar("--chart-line"),
  fill: cssVar("--chart-fill"), fillEnd: cssVar("--chart-fill-end"), up: cssVar("--chart-up"), down: cssVar("--chart-down"),
  amber: cssVar("--chart-amber"), crosshair: cssVar("--chart-crosshair"), label: cssVar("--chart-label"), bg: cssVar("--chart-bg"),
});
const ts = (iso: string) => Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;

function useChart(build: (chart: IChartApi, C: Palette) => void, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null);
  const mode = useSyncExternalStore(theme.subscribe, theme.get);
  useEffect(() => {
    if (!ref.current) return;
    const C = palette();
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" }, textColor: C.text, fontSize: 11,
        fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
      },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: C.crosshair, labelBackgroundColor: C.label, style: LineStyle.Dashed },
        horzLine: { color: C.crosshair, labelBackgroundColor: C.label, style: LineStyle.Dashed },
      },
      rightPriceScale: { borderColor: C.border },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: C.border },
    });
    build(chart, C);
    chart.timeScale().fitContent();
    return () => chart.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, mode]);
  return ref;
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const ref = useChart(
    (chart, C) => {
      const series = chart.addSeries(AreaSeries, {
        lineColor: C.line, lineWidth: 2, topColor: C.fill, bottomColor: C.fillEnd,
        priceLineColor: C.amber, crosshairMarkerBorderColor: C.line, crosshairMarkerBackgroundColor: C.bg,
      });
      series.setData(points.map((p) => ({ time: ts(p.ts), value: Number(p.equity) })));
    },
    [points],
  );
  return <div ref={ref} className="h-64 w-full min-w-0 overflow-hidden" />;
}

/** ``color``: "up" / "down" take the theme's money colours; anything else is used as given. */
export type Level = { price: string | null | undefined; label: string; color: string };
export type Mark = { time: string; kind: string; price: string; label: string };

export function PriceChart({ bars, markers = [], levels = [] }: { bars: BarOut[]; markers?: Mark[]; levels?: Level[] }) {
  const ref = useChart(
    (chart, C) => {
      const series = chart.addSeries(CandlestickSeries, {
        upColor: C.up, downColor: C.down, borderVisible: false, wickUpColor: C.up, wickDownColor: C.down,
      });
      series.setData(bars.map((b) => ({ time: ts(b.time), open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close) })));
      const marks: SeriesMarker<Time>[] = markers
        .map((m) => ({
          time: ts(m.time),
          position: m.kind === "entry" ? ("belowBar" as const) : ("aboveBar" as const),
          color: m.kind === "entry" ? C.line : C.amber,
          shape: m.kind === "entry" ? ("arrowUp" as const) : ("arrowDown" as const),
          text: m.label,
        }))
        .sort((a, b) => a.time - b.time);
      createSeriesMarkers(series, marks);
      for (const l of levels) {
        if (l.price) series.createPriceLine({ price: Number(l.price), color: l.color === "up" ? C.up : l.color === "down" ? C.down : l.color, lineStyle: LineStyle.Dashed, lineWidth: 1, title: l.label, axisLabelVisible: true });
      }
    },
    [bars, markers, levels],
  );
  return <div ref={ref} className="h-80 w-full min-w-0 overflow-hidden" />;
}
