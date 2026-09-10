"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_COLORS, hourLabel, shortDate } from "@/lib/format";

const AXIS = { fontSize: 11, fill: "var(--text-3)", fontFamily: "var(--font-mono)" };

// Recharts derives bar width from the category band. With a single data point that
// computation yields an empty rectangle - the bar silently disappears, which is
// exactly the state a freshly-deployed monitor is in. An explicit size avoids it.
//
// Two sizes because the charts have very different densities: a 24-hour chart in a
// half-width panel has ~25px per band, so a 28px bar would overlap its neighbours.
const BAR_SIZE = 28;   // sparse series: daily volume, route comparison, periods
const BAR_SIZE_DENSE = 12;  // 24 hourly buckets

// This dashboard re-polls every 60s; animating each refresh is noise, not feedback.
const NO_ANIM = { isAnimationActive: false } as const;
const TOOLTIP_STYLE = {
  background: "var(--raised)",
  border: "1.5px solid var(--rule)",
  borderRadius: 0,
  fontSize: 13,
  fontFamily: "var(--font-mono)",
  color: "var(--text)",
};

function pctTick(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function pctTooltip(value: unknown): string {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

function minTooltip(value: unknown): string {
  return typeof value === "number" ? `${value.toFixed(0)} min` : "—";
}

export interface DailySeriesPoint {
  stat_date: string;
  cancelled_flights: number;
  delayed_flights: number;
  total_flights: number;
  cancellation_rate: number | null;
  delay_rate: number | null;
  avg_delay_minutes: number | null;
}

/** Cancellations over time. */
export function CancellationsOverTime({ data }: { data: DailySeriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={data} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="stat_date" tickFormatter={shortDate} tick={AXIS} />
        <YAxis tick={AXIS} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={shortDate} />
        <Area
          {...NO_ANIM}
          type="monotone"
          dataKey="cancelled_flights"
          name="Cancelled"
          stroke={CHART_COLORS.cancel}
          fill={CHART_COLORS.cancel}
          fillOpacity={0.2}
          connectNulls={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** Average delay over time. */
export function DelaysOverTime({ data }: { data: DailySeriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={data} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="stat_date" tickFormatter={shortDate} tick={AXIS} />
        <YAxis tick={AXIS} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={shortDate}
          formatter={minTooltip}
        />
        <Line
          {...NO_ANIM}
          type="monotone"
          dataKey="avg_delay_minutes"
          name="Average delay"
          stroke={CHART_COLORS.delay}
          strokeWidth={2}
          dot={false}
          connectNulls={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Daily flight volume. */
export function DailyVolume({ data }: { data: DailySeriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="stat_date" tickFormatter={shortDate} tick={AXIS} />
        <YAxis tick={AXIS} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} labelFormatter={shortDate} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="total_flights" name="Flights" fill={CHART_COLORS.volume} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface HourlySeriesPoint {
  hour_local: number;
  total_flights: number;
  cancellation_rate: number | null;
  delay_rate: number | null;
  avg_delay_minutes: number | null;
}

/** Cancellation rate by local hour. */
export function CancellationRateByHour({ data }: { data: HourlySeriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 6, right: 8, left: -14, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="hour_local" tickFormatter={hourLabel} tick={AXIS} interval={1} />
        <YAxis tickFormatter={pctTick} tick={AXIS} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(h) => hourLabel(Number(h))}
          formatter={pctTooltip}
        />
        <Bar barSize={BAR_SIZE_DENSE} {...NO_ANIM} dataKey="cancellation_rate" name="Cancellation rate" radius={[3, 3, 0, 0]}>
          {data.map((point) => (
            <Cell
              key={point.hour_local}
              fill={
                point.cancellation_rate !== null && point.cancellation_rate > 0.1
                  ? CHART_COLORS.cancel
                  : CHART_COLORS.muted
              }
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Delay rate by local hour. */
export function DelayRateByHour({ data }: { data: HourlySeriesPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 6, right: 8, left: -14, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="hour_local" tickFormatter={hourLabel} tick={AXIS} interval={1} />
        <YAxis tickFormatter={pctTick} tick={AXIS} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(h) => hourLabel(Number(h))}
          formatter={pctTooltip}
        />
        <Bar barSize={BAR_SIZE_DENSE} {...NO_ANIM}
          dataKey="delay_rate"
          name="Delay rate"
          fill={CHART_COLORS.delay}
          radius={[3, 3, 0, 0]}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface RankedPoint {
  label: string;
  reliability_score: number | null;
  is_ranked: boolean;
  sample_size: number;
}

/** Airline reliability comparison. Unranked carriers are greyed out. */
export function AirlineReliability({ data }: { data: RankedPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={Math.max(220, data.length * 34)}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 6, right: 16, left: 8, bottom: 0 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis type="number" domain={[0, 100]} tick={AXIS} />
        <YAxis type="category" dataKey="label" width={140} tick={AXIS} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="reliability_score" name="Reliability" radius={[0, 3, 3, 0]}>
          {data.map((entry) => (
            <Cell
              key={entry.label}
              fill={entry.is_ranked ? CHART_COLORS.primary : CHART_COLORS.muted}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface RouteComparisonPoint {
  route: string;
  cancellation_rate: number | null;
  delay_rate: number | null;
  on_time_rate: number | null;
}

/** Route-versus-route comparison. */
export function RouteComparison({ data }: { data: RouteComparisonPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 6, right: 8, left: -14, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="route" tick={AXIS} />
        <YAxis tickFormatter={pctTick} tick={AXIS} />
        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={pctTooltip} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="on_time_rate" name="On time" fill={CHART_COLORS.onTime} radius={[3, 3, 0, 0]} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="delay_rate" name="Delayed" fill={CHART_COLORS.delay} radius={[3, 3, 0, 0]} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="cancellation_rate" name="Cancelled" fill={CHART_COLORS.cancel} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export interface PeriodPoint {
  key: string;
  total_flights: number;
  cancellation_rate: number | null;
  delay_rate: number | null;
  on_time_rate: number | null;
  avg_delay_minutes: number | null;
}

/** Time-of-day buckets: delay and cancellation rate side by side. */
export function TimeOfDayChart({ data }: { data: PeriodPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={280}>
      <BarChart data={data} margin={{ top: 6, right: 8, left: -14, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--rule-soft)" />
        <XAxis dataKey="key" tick={{ ...AXIS, fontSize: 10 }} />
        <YAxis tickFormatter={pctTick} tick={AXIS} />
        <Tooltip contentStyle={TOOLTIP_STYLE} formatter={pctTooltip} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM} dataKey="delay_rate" name="Delay rate" fill={CHART_COLORS.delay} radius={[3, 3, 0, 0]} />
        <Bar barSize={BAR_SIZE} {...NO_ANIM}
          dataKey="cancellation_rate"
          name="Cancellation rate"
          fill={CHART_COLORS.cancel}
          radius={[3, 3, 0, 0]}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
