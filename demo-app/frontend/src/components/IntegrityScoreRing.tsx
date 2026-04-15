import { useEffect, useState } from "react";
import { scoreColor, scoreLabel } from "../lib/status";

interface Props {
  score: number;
  size?: number;
  stroke?: number;
  sublabel?: string;
}

export default function IntegrityScoreRing({
  score,
  size = 180,
  stroke = 14,
  sublabel,
}: Props) {
  const [animated, setAnimated] = useState(0);

  useEffect(() => {
    const start = performance.now();
    const duration = 900;
    let frame = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setAnimated(Math.round(eased * score));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [score]);

  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const progress = (animated / 100) * circumference;
  const color = scoreColor(score);

  return (
    <div
      className="relative inline-flex items-center justify-center"
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          stroke="rgba(148,163,184,0.15)"
          fill="none"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={stroke}
          stroke={color}
          strokeLinecap="round"
          fill="none"
          strokeDasharray={circumference}
          strokeDashoffset={circumference - progress}
          style={{ transition: "stroke 300ms ease-out" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div
          className="text-4xl font-bold tabular-nums leading-none"
          style={{ color }}
        >
          {animated}
        </div>
        <div className="text-[11px] uppercase tracking-widest text-slate-400 mt-1">
          {sublabel ?? scoreLabel(score)}
        </div>
      </div>
    </div>
  );
}
