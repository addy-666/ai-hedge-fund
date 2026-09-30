import type { CalibrationSourceOut } from "../api/types";

const W = 240;
const H = 180;
const PAD = 26;
const x = (conf: number) => PAD + (conf / 100) * (W - 2 * PAD);
const y = (p: number) => H - PAD - p * (H - 2 * PAD);

/** Reliability diagram: stated confidence (x) vs how often those trades won (y); the diagonal is "honest".
 *  Dots are the bins (area ~ trades); the line is the ACTIVE calibration map, when there is one. */
export function Reliability({ source }: { source: CalibrationSourceOut }) {
  const bins = source.reliability.filter((b) => b.n > 0 && b.mean_confidence !== null && b.win_rate !== null);
  const most = Math.max(1, ...bins.map((b) => b.n));
  const points = source.active?.points ?? [];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-xs" role="img" aria-label={`${source.source} reliability`}>
      <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD} className="fill-zinc-950 stroke-zinc-800" />
      <line x1={x(0)} y1={y(0)} x2={x(100)} y2={y(1)} className="stroke-zinc-600" strokeDasharray="3 3" />
      {points.length > 1 && (
        <polyline
          data-testid="calibration-map"
          points={points.map(([c, p]) => `${x(c ?? 0)},${y(p ?? 0)}`).join(" ")}
          className="fill-none stroke-sky-400"
          strokeWidth={1.5}
        />
      )}
      {bins.map((b) => (
        <circle key={b.lo} cx={x(b.mean_confidence ?? 0)} cy={y(b.win_rate ?? 0)} r={2 + 5 * Math.sqrt(b.n / most)}
          className="fill-amber-400/70">
          <title>{`${b.lo}-${b.hi}: ${b.n} trades, stated ${b.mean_confidence}, won ${Math.round((b.win_rate ?? 0) * 100)}%`}</title>
        </circle>
      ))}
      <text x={W / 2} y={H - 6} textAnchor="middle" className="fill-zinc-500 text-[9px]">stated confidence</text>
      <text x={8} y={H / 2} textAnchor="middle" transform={`rotate(-90 8 ${H / 2})`} className="fill-zinc-500 text-[9px]">win rate</text>
      {[0, 50, 100].map((c) => <text key={c} x={x(c)} y={H - PAD + 10} textAnchor="middle" className="fill-zinc-600 text-[8px]">{c}</text>)}
    </svg>
  );
}
