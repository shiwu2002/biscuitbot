import { useId, useMemo } from "react";

import { cn } from "@/lib/utils";

export function formatCompactTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens >= 10_000_000 ? 0 : 1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(tokens >= 10_000 ? 0 : 1)}K`;
  return String(tokens);
}

type SeriesPoint = { label: string; value: number };

/* 霓虹配色：围绕 cyber-glow 青蓝展开，辅以电光紫/品红，明暗主题下均有足够对比。 */
const CHART_COLORS = [
  "hsl(192 92% 48%)",
  "hsl(260 85% 64%)",
  "hsl(204 88% 52%)",
  "hsl(285 85% 62%)",
  "hsl(172 72% 42%)",
  "hsl(220 85% 62%)",
];

export function UsageLineChart({
  data,
  ariaLabel,
  valueFormatter = formatCompactTokens,
  className,
}: {
  data: SeriesPoint[];
  ariaLabel: string;
  valueFormatter?: (value: number) => string;
  className?: string;
}) {
  const gradientId = useId();
  const { points, areaPath, linePath, max } = useMemo(() => {
    const width = 100;
    const height = 56;
    const padding = 4;
    const values = data.map((item) => item.value);
    const peak = Math.max(1, ...values);
    const step = data.length > 1 ? (width - padding * 2) / (data.length - 1) : 0;
    const coords = data.map((item, index) => {
      const x = padding + step * index;
      const ratio = item.value / peak;
      const y = height - padding - ratio * (height - padding * 2);
      return { x, y, item };
    });
    const line = coords
      .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`)
      .join(" ");
    const area = `${line} L ${coords[coords.length - 1]?.x ?? padding} ${height - padding} L ${padding} ${height - padding} Z`;
    return { points: coords, areaPath: area, linePath: line, max: peak };
  }, [data]);

  if (data.length === 0) {
    return (
      <div className={cn("flex h-32 items-center justify-center text-[12.5px] text-muted-foreground", className)}>
        —
      </div>
    );
  }

  return (
    <div className={cn("space-y-2", className)}>
      <svg
        viewBox="0 0 100 56"
        preserveAspectRatio="none"
        className="h-28 w-full"
        role="img"
        aria-label={ariaLabel}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" style={{ stopColor: "hsl(var(--cyber-glow-soft))", stopOpacity: 0.38 }} />
            <stop offset="100%" style={{ stopColor: "hsl(var(--cyber-glow-soft))", stopOpacity: 0.02 }} />
          </linearGradient>
        </defs>
        <path d={areaPath} fill={`url(#${gradientId})`} />
        <path
          d={linePath}
          fill="none"
          strokeWidth="1.5"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          style={{
            stroke: "hsl(var(--cyber-glow-soft))",
            filter: "drop-shadow(0 0 2.5px hsl(var(--cyber-glow) / 0.65))",
          }}
        />
        {points.map((point) => (
          <circle
            key={point.item.label}
            cx={point.x}
            cy={point.y}
            r="1.8"
            vectorEffect="non-scaling-stroke"
            style={{ fill: "hsl(var(--cyber-glow))" }}
          />
        ))}
      </svg>
      <div className="flex justify-between text-[10.5px] text-muted-foreground/75">
        <span>{data[0]?.label}</span>
        <span>{valueFormatter(max)}</span>
        <span>{data[data.length - 1]?.label}</span>
      </div>
    </div>
  );
}

export function UsageDistributionBars({
  items,
  ariaLabel,
  valueFormatter = formatCompactTokens,
  labelForKey,
}: {
  items: Array<{ key: string; value: number }>;
  ariaLabel: string;
  valueFormatter?: (value: number) => string;
  labelForKey?: (key: string) => string;
}) {
  const max = Math.max(1, ...items.map((item) => item.value));

  if (items.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-[12.5px] text-muted-foreground">
        —
      </div>
    );
  }

  return (
    <ul className="space-y-3" aria-label={ariaLabel}>
      {items.map((item, index) => {
        const width = `${Math.max(6, (item.value / max) * 100)}%`;
        const color = CHART_COLORS[index % CHART_COLORS.length];
        const label = labelForKey?.(item.key) ?? item.key;
        return (
          <li key={item.key}>
            <div className="mb-1 flex items-center justify-between gap-2 text-[12.5px]">
              <span className="truncate font-medium text-foreground">{label}</span>
              <span className="shrink-0 text-muted-foreground tabular-nums">{valueFormatter(item.value)}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-muted/70">
              <div
                className="h-full rounded-full"
                style={{
                  width,
                  background: `linear-gradient(90deg, ${color}, hsl(var(--cyber-glow)))`,
                  boxShadow: `0 0 10px ${color.replace(")", " / 0.45)")}`,
                }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
