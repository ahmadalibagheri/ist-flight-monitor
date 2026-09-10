/**
 * Display formatting.
 *
 * The single rule enforced here: an unknown value renders as an em dash, never
 * as 0 or 0%. "No data" and "zero" are different facts and the dashboard must
 * not blur them.
 */

export const NO_DATA = "—";

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  return `${(value * 100).toFixed(digits)}%`;
}

export function minutes(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  return `${value.toFixed(digits)} min`;
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  return value.toLocaleString();
}

export function score(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  return `${value.toFixed(0)}/100`;
}

export function signedPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(digits)}pp`;
}

export function signedMinutes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NO_DATA;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(0)} min`;
}

export function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00`;
}

/** Board-style day label, e.g. "Sun 06 Sep".
 *
 * Formatted in UTC because the incoming value is already a *local* calendar date
 * ("2026-09-06"); re-interpreting it in the viewer's zone would shift it a day.
 * The weekday is shown because day-of-week is one of the dimensions this product
 * analyses, so it is worth reading straight off the board.
 */
export function dayLabel(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  }).format(parsed);
}

export function shortDate(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Chart-safe numbers: Recharts renders `null` as a gap, which is what we want. */
export function chartValue(value: number | null | undefined): number | null {
  return value === null || value === undefined || Number.isNaN(value) ? null : value;
}

export const STATUS_COLORS: Record<string, string> = {
  SCHEDULED: "#4fd1a5",
  BOARDING: "#4fd1a5",
  DELAYED: "#f0a93b",
  DEPARTED: "#4fd1a5",
  ARRIVED: "#4fd1a5",
  CANCELLED: "#ff5c6c",
  DIVERTED: "#a78bfa",
  UNKNOWN: "#6f869b",
};

// Charts share the board's status vocabulary: amber always means delay, coral
// always means cancelled, wherever they appear.
export const CHART_COLORS = {
  primary: "#6ea8fe",
  delay: "#f0a93b",
  cancel: "#ff5c6c",
  onTime: "#4fd1a5",
  volume: "#6ea8fe",
  muted: "#3a5169",
};
