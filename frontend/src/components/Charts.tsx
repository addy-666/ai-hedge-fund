// lightweight-charts wrappers: the equity curve and a candlestick chart with trade markers and level lines.
import {
  CandlestickSeries, ColorType, createChart, createSeriesMarkers, LineSeries, LineStyle, type IChartApi,
  type SeriesMarker, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { BarOut, EquityPoint } from "../api/types";

const ts = (iso: string) => Math.floor(Date.parse(iso) / 1000) as UTCTimestamp;

function useChart(build: (chart: IChartApi) => void, deps: unknown[]) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#a1a1aa" },
      grid: { vertLines: { color: "#27272a" }, horzLines: { color: "#27272a" } },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    build(chart);
    chart.timeScale().fitContent();
    return () => chart.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const ref = useChart(
    (chart) => {
      const series = chart.addSeries(LineSeries, { color: "#34d399", lineWidth: 2 });
      series.setData(points.map((p) => ({ time: ts(p.ts), value: Number(p.equity) })));
    },
    [points],
  );
  return <div ref={ref} className="h-56 w-full" />;
}

export type Level = { price: string | null | undefined; label: string; color: string };
export type Mark = { time: string; kind: string; price: string; label: string };

export function PriceChart({ bars, markers = [], levels = [] }: { bars: BarOut[]; markers?: Mark[]; levels?: Level[] }) {
  const ref = useChart(
    (chart) => {
      const series = chart.addSeries(CandlestickSeries, {
        upColor: "#10b981", downColor: "#f43f5e", borderVisible: false, wickUpColor: "#10b981", wickDownColor: "#f43f5e",
      });
      series.setData(bars.map((b) => ({ time: ts(b.time), open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close) })));
      const marks: SeriesMarker<Time>[] = markers
        .map((m) => ({
          time: ts(m.time),
          position: m.kind === "entry" ? ("belowBar" as const) : ("aboveBar" as const),
          color: m.kind === "entry" ? "#38bdf8" : "#fbbf24",
          shape: m.kind === "entry" ? ("arrowUp" as const) : ("arrowDown" as const),
          text: m.label,
        }))
        .sort((a, b) => a.time - b.time);
      createSeriesMarkers(series, marks);
      for (const l of levels) {
        if (l.price) series.createPriceLine({ price: Number(l.price), color: l.color, lineStyle: LineStyle.Dashed, lineWidth: 1, title: l.label, axisLabelVisible: true });
      }
    },
    [bars, markers, levels],
  );
  return <div ref={ref} className="h-72 w-full" />;
}
