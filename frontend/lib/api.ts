/**
 * Typed client for the IST Flight Monitor API.
 *
 * Every metric that can be unknown is `number | null`, mirroring the backend
 * contract: a missing value is null, never 0. The UI must render those as an
 * em dash rather than a misleading zero.
 */

declare global {
  interface Window {
    __API_BASE__?: string;
  }
}

/**
 * Resolve the API base URL.
 *
 * `NEXT_PUBLIC_*` values are inlined at build time, which means a container image
 * built with one API URL would keep calling it even after the environment changed —
 * a silent, confusing failure. `app/layout.tsx` therefore injects the *runtime* value
 * as `window.__API_BASE__`, and that wins here. The build-time constant remains a
 * fallback for `next dev` and static rendering.
 */
export function apiBase(): string {
  const runtime = typeof window !== "undefined" ? window.__API_BASE__ : undefined;
  const configured = runtime || process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  return configured.replace(/\/$/, "");
}

export const API_PREFIX = "/api/v1";

export interface Metrics {
  total_flights: number;
  completed_flights: number;
  cancelled_flights: number;
  diverted_flights: number;
  delayed_flights: number;
  on_time_flights: number;
  unknown_flights: number;
  measurable_flights: number;
  delayed_gt_15: number;
  delayed_gt_30: number;
  delayed_gt_60: number;
  delayed_gt_120: number;
  avg_delay_minutes: number | null;
  median_delay_minutes: number | null;
  p90_delay_minutes: number | null;
  max_delay_minutes: number | null;
  cancellation_rate: number | null;
  delay_rate: number | null;
  on_time_rate: number | null;
  severe_delay_rate: number | null;
  reliability_score: number | null;
  sample_warning?: string | null;
}

export interface ScoreComponent {
  name: string;
  raw_value: number | null;
  normalised: number;
  weight: number;
  contribution: number;
  explanation: string;
}

export interface Reliability {
  score: number;
  sample_size: number;
  is_ranked: boolean;
  warning: string | null;
  components: ScoreComponent[];
}

export interface Summary extends Metrics {
  window: string;
  start_date: string | null;
  end_date: string | null;
  route: string | null;
  airline: string | null;
  bucket_percentages: Record<string, number>;
  reliability: Reliability | null;
}

export interface Ranked extends Metrics {
  key: string;
  label: string;
  sample_size: number;
  is_ranked: boolean;
  warning: string | null;
}

export interface HourlyPoint extends Metrics {
  hour_local: number;
  stat_date?: string | null;
}

export interface DailyPoint extends Metrics {
  stat_date: string;
  route: string | null;
}

export interface TimeOfDay {
  route: string;
  best_period: string | null;
  worst_delay_period: string | null;
  worst_cancellation_period: string | null;
  note: string | null;
  periods: Ranked[];
}

export interface Flight {
  id: number;
  flight_number: string;
  flight_iata: string | null;
  airline_iata: string | null;
  airline_name: string | null;
  origin_iata: string;
  destination_iata: string;
  scheduled_departure_utc: string;
  scheduled_departure_local: string | null;
  first_scheduled_departure_utc: string | null;
  schedule_moved_minutes: number | null;
  total_displacement_minutes: number | null;
  actual_departure_utc: string | null;
  flight_date_local: string;
  scheduled_hour_local: number;
  status: string;
  delay_minutes: number | null;
  is_cancelled: boolean;
  cancellation_reason: string | null;
  is_diverted: boolean;
  terminal: string | null;
  gate: string | null;
  aircraft_type: string | null;
  data_source: string;
  data_quality: string;
  observation_count: number;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface RouteInfo {
  route: string;
  origin_iata: string;
  destination_iata: string;
  destination_name: string | null;
  destination_city: string | null;
  total_flights: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface Airline {
  iata: string | null;
  icao: string | null;
  name: string;
}

export interface Provider {
  name: string;
  kind: "REAL" | "MOCK";
  description: string;
  configured: boolean;
  in_chain: boolean;
  chain_position: number | null;
  is_healthy: boolean | null;
  consecutive_failures: number | null;
  last_success_at: string | null;
  last_error: string | null;
}

export interface RepeatCancellation {
  flight_number: string;
  airline: string | null;
  route: string;
  scheduled_local_time: string;
  cancellations: number;
  scheduled_occasions: number;
  dates: string[];
  cancellation_rate: number;
}

export interface Trend {
  route: string;
  window_days: number;
  current_period: { start: string; end: string };
  previous_period: { start: string; end: string };
  current: Record<string, number | null>;
  previous: Record<string, number | null>;
  deltas: Record<string, number | null>;
  sufficient_data: boolean;
}

export interface HourlyReport {
  generated_at_local: string;
  local_date: string;
  history_days: number;
  routes: (Metrics & {
    route: string;
    destination_name: string;
    departed: number;
    scheduled_remaining: number;
  })[];
  cancelled: FlightIssue[];
  delayed: FlightIssue[];
  period_verdicts: TimeOfDay[];
  best_flight: Ranked | null;
  worst_flight: Ranked | null;
  data_note: string | null;
  text: string | null;
}

export interface FlightIssue {
  flight_number: string;
  airline: string;
  scheduled_local: string;
  destination: string;
  status: string;
  delay_minutes: number | null;
  reason: string | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Fetch JSON from the API, turning non-2xx responses into a typed error. */
export async function fetcher<T>(path: string): Promise<T> {
  const base = apiBase();
  const url = path.startsWith("http") ? path : `${base}${path}`;
  let response: Response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (cause) {
    throw new ApiError(`Cannot reach the API at ${base}. Is the backend running?`, 0, cause);
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError(
      `API request failed (${response.status}) for ${path}`,
      response.status,
      detail,
    );
  }
  return (await response.json()) as T;
}

export interface Filters {
  window: string;
  startDate?: string;
  endDate?: string;
  route?: string;
  airline?: string;
  flightNumber?: string;
}

/** Serialise the shared dashboard filters into a query string. */
export function toQuery(filters: Filters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams();
  if (filters.startDate && filters.endDate) {
    params.set("start_date", filters.startDate);
    params.set("end_date", filters.endDate);
  } else {
    params.set("window", filters.window);
  }
  if (filters.route) params.set("route", filters.route);
  if (filters.airline) params.set("airline", filters.airline);
  for (const [key, value] of Object.entries(extra)) params.set(key, value);
  return params.toString();
}

/** Local calendar date in the operational timezone, as `YYYY-MM-DD`. */
function localDate(offsetDays = 0): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Istanbul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "01";
  const base = new Date(`${get("year")}-${get("month")}-${get("day")}T00:00:00Z`);
  base.setUTCDate(base.getUTCDate() + offsetDays);
  return base.toISOString().slice(0, 10);
}

/** Turn a named window (or an explicit range) into concrete local dates. */
export function resolveWindow(f: Filters): { start: string; end: string } | null {
  if (f.startDate && f.endDate) return { start: f.startDate, end: f.endDate };
  const today = localDate();
  const spans: Record<string, number> = { today: 1, "7d": 7, "30d": 30, "90d": 90 };
  if (f.window === "mtd") return { start: `${today.slice(0, 7)}-01`, end: today };
  if (f.window === "all") return null;   // no bound; let the API return everything
  const days = spans[f.window] ?? 30;
  return { start: localDate(-(days - 1)), end: today };
}

export const endpoints = {
  statistics: (f: Filters) => `${API_PREFIX}/statistics?${toQuery(f)}`,
  hourly: (f: Filters) => `${API_PREFIX}/statistics/hourly?${toQuery(f, { from_cache: "false" })}`,
  daily: (f: Filters) => `${API_PREFIX}/statistics/daily?${toQuery(f)}`,
  timeOfDay: (f: Filters) => `${API_PREFIX}/statistics/time-of-day?${toQuery(f)}`,
  airlines: (f: Filters) => `${API_PREFIX}/statistics/airlines?${toQuery(f)}`,
  repeatCancellations: (f: Filters) =>
    `${API_PREFIX}/statistics/repeat-cancellations?${toQuery(f)}`,
  flightRanking: (f: Filters) => `${API_PREFIX}/statistics/flights?${toQuery(f)}`,
  trends: (days: number, route?: string) =>
    `${API_PREFIX}/statistics/trends?days=${days}${route ? `&route=${route}` : ""}`,
  /**
   * `GET /flights` filters on explicit local dates, not the named windows that
   * `/statistics` accepts — passing `window=today` there is silently ignored, so a
   * panel asking for today would quietly receive every flight ever collected.
   * Named windows are therefore resolved to concrete dates here.
   */
  flights: (f: Filters, limit = 100, offset = 0) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    const range = resolveWindow(f);
    if (range) {
      params.set("start_date", range.start);
      params.set("end_date", range.end);
    }
    if (f.route) params.set("route", f.route);
    if (f.airline) params.set("airline", f.airline);
    if (f.flightNumber) params.set("flight_number", f.flightNumber);
    return `${API_PREFIX}/flights?${params.toString()}`;
  },
  routes: () => `${API_PREFIX}/routes`,
  airlineList: () => `${API_PREFIX}/airlines`,
  report: () => `${API_PREFIX}/reports/latest`,
  providers: () => `/providers`,
  health: () => `/health`,
};
