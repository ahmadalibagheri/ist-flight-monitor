"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import {
  AirlineReliability,
  CancellationRateByHour,
  CancellationsOverTime,
  DailyVolume,
  DelayRateByHour,
  DelaysOverTime,
  RouteComparison,
  type RouteComparisonPoint,
  TimeOfDayChart,
} from "@/components/charts";
import {
  Board,
  BoardLegend,
  Cluster,
  FeedError,
  Message,
  Panel,
  Reading,
  Sample,
  Slot,
  Vacant,
  Waiting,
} from "@/components/ui";
import {
  type DailyPoint,
  type Filters,
  type Flight,
  type HourlyPoint,
  type HourlyReport,
  type Page,
  type Ranked,
  type RouteInfo,
  type RepeatCancellation,
  type Summary,
  type TimeOfDay,
  type Trend,
  endpoints,
  routeLabel,
  fetcher,
  routePair,
} from "@/lib/api";
import {
  STATUS_COLORS,
  count,
  minutes,
  pct,
  score,
  shortDate,
  signedMinutes,
  signedPct,
} from "@/lib/format";

// Reading draws a slot for null; these keep "unknown" flowing through as null
// rather than being flattened into a dash string.
const pctOrNull = (v: number | null | undefined) => (v === null || v === undefined ? null : pct(v));
const minutesOrNull = (v: number | null | undefined) =>
  v === null || v === undefined ? null : minutes(v);
const scoreOrNull = (v: number | null | undefined) =>
  v === null || v === undefined ? null : score(v);

const REFRESH_MS = 60_000;

function useApi<T>(key: string | null) {
  return useSWR<T, Error>(key, fetcher<T>, {
    refreshInterval: REFRESH_MS,
    revalidateOnFocus: false,
    keepPreviousData: true,
  });
}

/** Shared guard so no section renders a chart over an error or a loading state. */
function Guard<T>({
  result,
  children,
  label,
}: {
  result: { data?: T; error?: Error; isLoading: boolean };
  children: (data: T) => JSX.Element;
  label?: string;
}) {
  if (result.error) return <FeedError error={result.error} />;
  if (result.isLoading || result.data === undefined) return <Waiting label={label} />;
  return children(result.data);
}

function headline(summary: Summary) {
  const cancelTone =
    summary.cancellation_rate === null
      ? undefined
      : summary.cancellation_rate > 0.1
        ? ("stop" as const)
        : summary.cancellation_rate > 0.05
          ? ("caution" as const)
          : ("clear" as const);
  const delayTone =
    summary.delay_rate === null
      ? undefined
      : summary.delay_rate > 0.4
        ? ("stop" as const)
        : summary.delay_rate > 0.25
          ? ("caution" as const)
          : ("clear" as const);
  return { cancelTone, delayTone };
}

/** The instrument cluster: the numbers that decide whether to worry.
 *
 * Six readings, not eight cards. Rates that cannot be measured show a slot, so a
 * window with no completed flights never reads as a good one.
 */
function SummaryCluster({ summary }: { summary: Summary }) {
  const { cancelTone, delayTone } = headline(summary);
  const unmeasured =
    summary.unknown_flights > 0
      ? `${summary.unknown_flights} without usable timing, excluded from rates`
      : undefined;

  return (
    <Cluster>
      <Reading
        name="Flights"
        value={count(summary.total_flights)}
        qualifier={`${count(summary.measurable_flights)} with a measurable delay`}
      />
      <Reading
        name="Cancelled"
        value={summary.cancellation_rate === null ? null : pct(summary.cancellation_rate)}
        qualifier={`${count(summary.cancelled_flights)} of ${count(summary.total_flights)}`}
        tone={cancelTone}
      />
      <Reading
        name="Delayed"
        value={summary.delay_rate === null ? null : pct(summary.delay_rate)}
        qualifier={`${count(summary.delayed_flights)} past the threshold`}
        tone={delayTone}
      />
      <Reading
        name="To time"
        value={summary.on_time_rate === null ? null : pct(summary.on_time_rate)}
        qualifier={unmeasured}
        tone={summary.on_time_rate !== null && summary.on_time_rate > 0.75 ? "clear" : undefined}
      />
      <Reading
        name="Typical delay"
        value={summary.avg_delay_minutes === null ? null : minutes(summary.avg_delay_minutes)}
        qualifier={
          summary.median_delay_minutes === null
            ? undefined
            : `median ${minutes(summary.median_delay_minutes)}, worst ${minutes(summary.max_delay_minutes)}`
        }
      />
      <Reading
        name="Reliability"
        value={summary.reliability_score === null ? null : score(summary.reliability_score)}
        qualifier={summary.reliability?.warning ?? undefined}
      />
    </Cluster>
  );
}

function NoDataNotice({ summary }: { summary: Summary }) {
  if (summary.total_flights > 0) return null;
  return (
    <Message tone="caution" title="Nothing collected for this window">
      Widen the date range, or check that a provider is configured and a collection cycle has
      run. Synthetic development data is never counted here.
    </Message>
  );
}

/* ------------------------------------------------------------------ 1. Overview */
export function OverviewSection({ filters }: { filters: Filters }) {
  const summary = useApi<Summary>(endpoints.statistics(filters));
  const daily = useApi<DailyPoint[]>(endpoints.daily(filters));
  const report = useApi<HourlyReport>(endpoints.report());
  // The board shows today, whatever window the statistics below are using -
  // "what is happening now" and "what usually happens" are different questions.
  const board = useApi<Page<Flight>>(
    endpoints.flights({ window: "today", route: filters.route }, 60),
  );

  return (
    <>
      <Panel title="Departing today" legend flush>
        <Guard result={board} label="Reading the departure board">
          {(data) => <Board flights={[...data.items].reverse()} />}
        </Guard>
      </Panel>

      <Guard result={summary} label="Reading statistics">
        {(data) => (
          <>
            <NoDataNotice summary={data} />
            <SummaryCluster summary={data} />
          </>
        )}
      </Guard>

      <div className="split">
        <Panel title="Flights per day">
          <Guard result={daily}>
            {(data) =>
              data.length ? <DailyVolume data={data} /> : <Vacant>No days in range.</Vacant>
            }
          </Guard>
        </Panel>
        <Panel title="Typical delay by day">
          <Guard result={daily}>
            {(data) =>
              data.length ? <DelaysOverTime data={data} /> : <Vacant>No days in range.</Vacant>
            }
          </Guard>
        </Panel>
      </div>

      <Panel title="Hourly report" note="The text delivered to Telegram each hour.">
        <Guard result={report}>
          {(data) =>
            data.text ? (
              <pre className="printout">{data.text}</pre>
            ) : (
              <Vacant>Report unavailable.</Vacant>
            )
          }
        </Guard>
      </Panel>
    </>
  );
}

/* --------------------------------------------------- 2 & 3. Per-route sections */
export function RouteSection({
  filters,
  route,
  title,
  bare,
}: {
  filters: Filters;
  route: string;
  title: string;
  /** Suppress the built-in heading when the caller has already labelled the route. */
  bare?: boolean;
}) {
  const scoped: Filters = { ...filters, route };
  const summary = useApi<Summary>(endpoints.statistics(scoped));
  const daily = useApi<DailyPoint[]>(endpoints.daily(scoped));
  const hourly = useApi<HourlyPoint[]>(endpoints.hourly(scoped));
  const tod = useApi<TimeOfDay[]>(endpoints.timeOfDay(scoped));

  return (
    <>
      {!bare && (
        <div className="heading">
          {title && title !== routePair(route)
            ? `${title} — ${routePair(route)}`
            : routePair(route)}
        </div>
      )}
      <Guard result={summary}>
        {(data) => (
          <>
            <NoDataNotice summary={data} />
            <SummaryCluster summary={data} />
          </>
        )}
      </Guard>

      <div className="split" style={{ marginTop: 14 }}>
        <Panel title="Cancellations over time">
          <Guard result={daily}>
            {(data) =>
              data.length ? (
                <CancellationsOverTime data={data} />
              ) : (
                <Vacant>No data in range.</Vacant>
              )
            }
          </Guard>
        </Panel>
        <Panel
          title="Delay rate by hour of day"
          note="Hours are bucketed in the monitor's operational timezone."
        >
          <Guard result={hourly}>{(data) => <DelayRateByHour data={data} />}</Guard>
        </Panel>
      </div>

      <div className="heading">Best and worst times</div>
      <Guard result={tod}>
        {(data) => {
          const verdict = data.find((entry) => entry.route === route);
          if (!verdict) return <Vacant>No time-of-day data.</Vacant>;
          return <TimeOfDayPanel verdict={verdict} />;
        }}
      </Guard>
    </>
  );
}

/* --------------------------------------------------- Return legs (own page) */
/**
 * Every leg heading back to the hub, on one page of its own.
 *
 * These deliberately do not share the outbound tabs. In a flat list the only thing
 * separating "Tehran → Istanbul" from "Istanbul → Tehran" is a small arrow, and
 * reading the wrong one means reading the wrong direction's cancellation rate. The
 * banner and the per-route heading both restate the direction, because someone
 * arriving here from a bookmark has no other context.
 */
export function ReturnsSection({
  filters,
  routes,
  hubLabel,
}: {
  filters: Filters;
  routes: RouteInfo[];
  hubLabel: string;
}) {
  if (routes.length === 0) {
    return (
      <Vacant>
        No return legs are being collected. Add one to <code>MONITORED_ROUTES</code>,
        for example <code>IKA-IST</code>.
      </Vacant>
    );
  }

  return (
    <>
      <Message tone="info" title={`Flights back into ${hubLabel}`}>
        The opposite direction to the outbound tabs, counted separately. A carrier&apos;s
        punctuality leaving {hubLabel} says little about its return, so these figures
        share nothing with the outbound ones — including the reliability score.
      </Message>

      {routes.map((entry) => (
        <div key={entry.route}>
          <div className="heading">
            {routeLabel(entry)}{" "}
            <span className="chip">return · {routePair(entry.route)}</span>
          </div>
          <RouteSection filters={filters} route={entry.route} title={routeLabel(entry)} bare />
        </div>
      ))}
    </>
  );
}

/* ------------------------------------------------------------------ 4. Airlines */
export function AirlinesSection({ filters }: { filters: Filters }) {
  const airlines = useApi<Ranked[]>(endpoints.airlines(filters));

  return (
    <Guard result={airlines} label="Ranking airlines…">
      {(data) =>
        data.length === 0 ? (
          <Vacant>No airline data for these filters.</Vacant>
        ) : (
          <>
            <Message title="How airlines are ranked">
              Carriers below the configured minimum sample size are shown in grey, marked
              <em> unranked</em>, and sorted last — a carrier with two lucky flights never tops
              the table.
            </Message>
            <Panel title="Reliability score by airline">
              <AirlineReliability data={data} />
            </Panel>
            <Panel title="Detail" note="Rates are shares of measurable flights unless noted.">
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Airline</th>
                      <th className="num">Flights</th>
                      <th className="num">Cancelled</th>
                      <th className="num">Cancel rate</th>
                      <th className="num">Delayed</th>
                      <th className="num">Delay rate</th>
                      <th className="num">On time</th>
                      <th className="num">Avg delay</th>
                      <th className="num">Median</th>
                      <th className="num">Score</th>
                      <th>Sample</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.map((airline) => (
                      <tr key={airline.key}>
                        <td>{airline.label}</td>
                        <td className="num">{count(airline.total_flights)}</td>
                        <td className="num">{count(airline.cancelled_flights)}</td>
                        <td className="num">{pct(airline.cancellation_rate)}</td>
                        <td className="num">{count(airline.delayed_flights)}</td>
                        <td className="num">{pct(airline.delay_rate)}</td>
                        <td className="num">{pct(airline.on_time_rate)}</td>
                        <td className="num">{minutes(airline.avg_delay_minutes)}</td>
                        <td className="num">{minutes(airline.median_delay_minutes)}</td>
                        <td className="num">{score(airline.reliability_score)}</td>
                        <td>
                          <Sample ranked={airline.is_ranked} size={airline.sample_size} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          </>
        )
      }
    </Guard>
  );
}

/* ------------------------------------------------------------------- 5. Flights */
export function FlightsSection({ filters }: { filters: Filters }) {
  const flights = useApi<Page<Flight>>(endpoints.flights(filters, 200));
  const ranking = useApi<Ranked[]>(endpoints.flightRanking(filters));

  return (
    <>
      <Guard result={ranking}>
        {(data) => {
          const ranked = data.filter((entry) => entry.is_ranked);
          if (!ranked.length) {
            return (
              <Message tone="info" title="Not enough history to rank flights yet">
                A flight number needs a run of observed departures before it can be
                compared. Keep collecting.
              </Message>
            );
          }
          const best = ranked[0];
          const worst = ranked[ranked.length - 1];
          return (
            <Cluster>
              <Reading
                name="Most reliable"
                value={best.key}
                qualifier={`${score(best.reliability_score)} over ${best.sample_size} flights`}
                tone="clear"
              />
              <Reading
                name="Least reliable"
                value={worst.key}
                qualifier={`${score(worst.reliability_score)} over ${worst.sample_size} flights`}
                tone="stop"
              />
            </Cluster>
          );
        }}
      </Guard>

      <Panel
        title="Every flight observed"
        note="Newest first."
        legend
        flush
      >
        <Guard result={flights} label="Reading flights">
          {(data) => <Board flights={data.items} showDate />}
        </Guard>
      </Panel>

      <Guard result={flights}>
        {(data) => (
          <p className="note" style={{ padding: "0 2px" }}>
            Showing {data.items.length} of {count(data.total)}.
          </p>
        )}
      </Guard>
    </>
  );
}

/* -------------------------------------------------------------------- 6. Delays */
export function DelaysSection({ filters }: { filters: Filters }) {
  const summary = useApi<Summary>(endpoints.statistics(filters));
  const hourly = useApi<HourlyPoint[]>(endpoints.hourly(filters));
  const daily = useApi<DailyPoint[]>(endpoints.daily(filters));

  return (
    <>
      <Guard result={summary}>
        {(data) => (
          <div className="cluster">
            <Reading name="Average delay" value={minutesOrNull(data.avg_delay_minutes)} />
            <Reading name="Median delay" value={minutesOrNull(data.median_delay_minutes)} />
            <Reading name="90th percentile" value={minutesOrNull(data.p90_delay_minutes)} />
            <Reading name="Worst delay" value={minutesOrNull(data.max_delay_minutes)} />
            <Reading
              name="Delayed > 15 min"
              value={pctOrNull(data.measurable_flights ? data.delayed_gt_15 / data.measurable_flights : null)}
              qualifier={`${count(data.delayed_gt_15)} flights`}
            />
            <Reading
              name="Delayed > 30 min"
              value={pctOrNull(data.measurable_flights ? data.delayed_gt_30 / data.measurable_flights : null)}
              qualifier={`${count(data.delayed_gt_30)} flights`}
            />
            <Reading
              name="Delayed > 60 min"
              value={pctOrNull(data.measurable_flights ? data.delayed_gt_60 / data.measurable_flights : null)}
              qualifier={`${count(data.delayed_gt_60)} flights`}
              tone="caution"
            />
            <Reading
              name="Delayed > 120 min"
              value={pctOrNull(data.measurable_flights ? data.delayed_gt_120 / data.measurable_flights : null)}
              qualifier={`${count(data.delayed_gt_120)} flights`}
              tone="stop"
            />
          </div>
        )}
      </Guard>

      <div className="split" style={{ marginTop: 14 }}>
        <Panel
          title="Delay rate by hour of day"
          note="Hours are bucketed in the monitor's operational timezone."
        >
          <Guard result={hourly}>{(data) => <DelayRateByHour data={data} />}</Guard>
        </Panel>
        <Panel title="Average delay over time">
          <Guard result={daily}>
            {(data) =>
              data.length ? <DelaysOverTime data={data} /> : <Vacant>No data in range.</Vacant>
            }
          </Guard>
        </Panel>
      </div>
    </>
  );
}

/* ------------------------------------------------------------- 7. Cancellations */
export function CancellationsSection({ filters }: { filters: Filters }) {
  const summary = useApi<Summary>(endpoints.statistics(filters));
  const hourly = useApi<HourlyPoint[]>(endpoints.hourly(filters));
  const daily = useApi<DailyPoint[]>(endpoints.daily(filters));
  const report = useApi<HourlyReport>(endpoints.report());
  const repeats = useApi<RepeatCancellation[]>(endpoints.repeatCancellations(filters));

  return (
    <>
      <Guard result={repeats}>
        {(data) =>
          data.length === 0 ? (
            <></>
          ) : (
            <Panel
              title="Cancelled repeatedly"
              note="Shown whatever the sample size — a flight that never operates is a finding, not noise."
              flush
            >
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Flight</th>
                      <th>Airline</th>
                      <th>Departs</th>
                      <th className="num">Cancelled</th>
                      <th className="num">Rate</th>
                      <th>Dates</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.map((r) => (
                      <tr key={r.flight_number}>
                        <td className="mono">{r.flight_number}</td>
                        <td>{r.airline ?? "Unknown"}</td>
                        <td className="mono">{r.scheduled_local_time}</td>
                        <td className="num">
                          {r.cancellations} of {r.scheduled_occasions} days
                        </td>
                        <td className="num">{pct(r.cancellation_rate, 0)}</td>
                        <td className="mono">{r.dates.join(", ")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          )
        }
      </Guard>

      <Guard result={summary}>
        {(data) => (
          <div className="cluster">
            <Reading name="Total flights" value={count(data.total_flights)} />
            <Reading name="Cancelled" value={count(data.cancelled_flights)} tone="stop" />
            <Reading
              name="Cancellation rate"
              value={pctOrNull(data.cancellation_rate)}
              qualifier="Share of all scheduled flights"
            />
            <Reading name="Diverted" value={count(data.diverted_flights)} />
          </div>
        )}
      </Guard>

      <div className="split" style={{ marginTop: 14 }}>
        <Panel title="Cancellation rate by hour of day" note="Bars above 10% are highlighted.">
          <Guard result={hourly}>{(data) => <CancellationRateByHour data={data} />}</Guard>
        </Panel>
        <Panel title="Cancellations over time">
          <Guard result={daily}>
            {(data) =>
              data.length ? (
                <CancellationsOverTime data={data} />
              ) : (
                <Vacant>No data in range.</Vacant>
              )
            }
          </Guard>
        </Panel>
      </div>

      <div className="heading">Currently cancelled</div>
      <Panel>
        <Guard result={report}>
          {(data) =>
            data.cancelled.length === 0 ? (
              <Vacant>No cancellations recorded for today.</Vacant>
            ) : (
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Flight</th>
                      <th>Airline</th>
                      <th>Scheduled</th>
                      <th>Destination</th>
                      <th>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.cancelled.map((issue) => (
                      <tr key={`${issue.flight_number}-${issue.scheduled_local}`}>
                        <td>{issue.flight_number}</td>
                        <td>{issue.airline}</td>
                        <td>{issue.scheduled_local}</td>
                        <td>{issue.destination}</td>
                        <td>{issue.reason ?? "Not published by provider"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </Guard>
      </Panel>
    </>
  );
}

/* -------------------------------------------------------------- 8. Time of day */
function TimeOfDayPanel({ verdict }: { verdict: TimeOfDay }) {
  return (
    <>
      {verdict.note && <Message tone="caution" title="Not enough data to rank">{verdict.note}</Message>}
      <div className="cluster">
        <Reading name="Best time to fly" value={verdict.best_period ?? "—"} tone="clear" />
        <Reading name="Worst for delays" value={verdict.worst_delay_period ?? "—"} tone="caution" />
        <Reading
          name="Worst for cancellations"
          value={verdict.worst_cancellation_period ?? "—"}
          tone="stop"
        />
      </div>
      <Panel title="By time-of-day window" note="Hours are bucketed in the monitor's operational timezone.">
        <TimeOfDayChart data={verdict.periods} />
        <div className="scroll" style={{ marginTop: 12 }}>
          <table>
            <thead>
              <tr>
                <th>Period</th>
                <th className="num">Flights</th>
                <th className="num">Cancel rate</th>
                <th className="num">Delay rate</th>
                <th className="num">On time</th>
                <th className="num">Avg delay</th>
                <th className="num">Median</th>
                <th className="num">Score</th>
                <th>Sample</th>
              </tr>
            </thead>
            <tbody>
              {verdict.periods.map((period) => (
                <tr key={period.key}>
                  <td>{period.key}</td>
                  <td className="num">{count(period.total_flights)}</td>
                  <td className="num">{pct(period.cancellation_rate)}</td>
                  <td className="num">{pct(period.delay_rate)}</td>
                  <td className="num">{pct(period.on_time_rate)}</td>
                  <td className="num">{minutes(period.avg_delay_minutes)}</td>
                  <td className="num">{minutes(period.median_delay_minutes)}</td>
                  <td className="num">{score(period.reliability_score)}</td>
                  <td>
                    <Sample ranked={period.is_ranked} size={period.sample_size} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}

export function TimeOfDaySection({ filters }: { filters: Filters }) {
  const tod = useApi<TimeOfDay[]>(endpoints.timeOfDay(filters));
  const hourly = useApi<HourlyPoint[]>(endpoints.hourly(filters));

  return (
    <>
      <div className="split">
        <Panel title="Delay rate by hour">
          <Guard result={hourly}>{(data) => <DelayRateByHour data={data} />}</Guard>
        </Panel>
        <Panel title="Cancellation rate by hour">
          <Guard result={hourly}>{(data) => <CancellationRateByHour data={data} />}</Guard>
        </Panel>
      </div>

      <Guard result={tod} label="Analysing time-of-day patterns…">
        {(data) => (
          <>
            {data.map((verdict) => (
              <div key={verdict.route}>
                <div className="heading">{routePair(verdict.route)}</div>
                <TimeOfDayPanel verdict={verdict} />
              </div>
            ))}
          </>
        )}
      </Guard>
    </>
  );
}

/* ------------------------------------------------------------- 9. Historical */

/**
 * Fetch one route's summary for the comparison chart.
 *
 * The comparison covers however many routes are configured, and a hook cannot be
 * called in a loop — so each route gets its own component instance with its own
 * fixed set of hooks, and reports its point up to the parent, which draws the single
 * combined chart. This renders nothing itself.
 */
function RouteComparisonProbe({
  filters,
  route,
  onPoint,
}: {
  filters: Filters;
  route: string;
  onPoint: (route: string, point: RouteComparisonPoint | null) => void;
}) {
  const { data } = useApi<Summary>(endpoints.statistics({ ...filters, route }));

  useEffect(() => {
    onPoint(
      route,
      data
        ? {
            // The axis label has to fit several routes, so it uses the directional
            // IATA pair rather than the (much longer) city pair.
            route: routePair(route),
            cancellation_rate: data.cancellation_rate,
            delay_rate: data.delay_rate,
            on_time_rate: data.on_time_rate,
          }
        : null,
    );
  }, [route, data, onPoint]);

  return null;
}

export function TrendsSection({ filters }: { filters: Filters }) {
  const trends = useApi<Trend[]>(endpoints.trends(30));
  const daily = useApi<DailyPoint[]>(endpoints.daily(filters));
  const routes = useApi<RouteInfo[]>(endpoints.routes());

  const [points, setPoints] = useState<Record<string, RouteComparisonPoint | null>>({});
  // Stable identity: the probes' effects must not re-run on every parent render.
  const onPoint = useCallback((route: string, point: RouteComparisonPoint | null) => {
    setPoints((previous) =>
      previous[route] === point ? previous : { ...previous, [route]: point },
    );
  }, []);

  const known = routes.data ?? [];
  // Driven by the route list, so a point left over from a route that is no longer
  // collected is ignored rather than charted.
  const comparison = known
    .map((entry) => points[entry.route])
    .filter((point): point is RouteComparisonPoint => Boolean(point));
  const complete = known.length > 0 && comparison.length === known.length;

  return (
    <>
      <Panel
        title="Route comparison"
        note="Rates over the selected window. On-time and delay rates share the measurable-flight denominator."
      >
        {known.map((entry) => (
          <RouteComparisonProbe
            key={entry.route}
            filters={filters}
            route={entry.route}
            onPoint={onPoint}
          />
        ))}
        {routes.error ? (
          <FeedError error={routes.error} />
        ) : complete ? (
          <RouteComparison data={comparison} />
        ) : known.length === 0 && !routes.isLoading ? (
          <Vacant>No routes are being collected.</Vacant>
        ) : (
          <Waiting label="Comparing routes" />
        )}
      </Panel>

      <div className="heading">What is changing</div>
      <Guard result={trends} label="Computing trends…">
        {(data) => (
          <div className="split">
            {data.map((entry) => (
              <Panel
                key={entry.route}
                title={routePair(entry.route)}
                note={`Last ${entry.window_days} days vs the ${entry.window_days} before. A positive change means it got worse, except on-time rate.`}
              >
                {!entry.sufficient_data && (
                  <Message tone="caution" title="Thin data">
                    One or both periods are below the minimum sample size, so these movements
                    may be noise.
                  </Message>
                )}
                <div className="scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Metric</th>
                        <th className="num">Previous</th>
                        <th className="num">Current</th>
                        <th className="num">Change</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td>Flights</td>
                        <td className="num">{count(entry.previous.total_flights)}</td>
                        <td className="num">{count(entry.current.total_flights)}</td>
                        <td className="num">{count(entry.deltas.total_flights)}</td>
                      </tr>
                      <tr>
                        <td>Cancellation rate</td>
                        <td className="num">{pct(entry.previous.cancellation_rate)}</td>
                        <td className="num">{pct(entry.current.cancellation_rate)}</td>
                        <td className="num">{signedPct(entry.deltas.cancellation_rate)}</td>
                      </tr>
                      <tr>
                        <td>Delay rate</td>
                        <td className="num">{pct(entry.previous.delay_rate)}</td>
                        <td className="num">{pct(entry.current.delay_rate)}</td>
                        <td className="num">{signedPct(entry.deltas.delay_rate)}</td>
                      </tr>
                      <tr>
                        <td>On-time rate</td>
                        <td className="num">{pct(entry.previous.on_time_rate)}</td>
                        <td className="num">{pct(entry.current.on_time_rate)}</td>
                        <td className="num">{signedPct(entry.deltas.on_time_rate)}</td>
                      </tr>
                      <tr>
                        <td>Average delay</td>
                        <td className="num">{minutes(entry.previous.avg_delay_minutes)}</td>
                        <td className="num">{minutes(entry.current.avg_delay_minutes)}</td>
                        <td className="num">{signedMinutes(entry.deltas.avg_delay_minutes)}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </Panel>
            ))}
          </div>
        )}
      </Guard>

      <div className="heading">History</div>
      <div className="split">
        <Panel title="Daily flight volume">
          <Guard result={daily}>
            {(data) =>
              data.length ? <DailyVolume data={data} /> : <Vacant>No days in range.</Vacant>
            }
          </Guard>
        </Panel>
        <Panel title="Cancellations over time">
          <Guard result={daily}>
            {(data) =>
              data.length ? (
                <CancellationsOverTime data={data} />
              ) : (
                <Vacant>No days in range.</Vacant>
              )
            }
          </Guard>
        </Panel>
      </div>
      <Panel title="Daily detail">
        <Guard result={daily}>
          {(data) =>
            data.length === 0 ? (
              <Vacant>No days in range.</Vacant>
            ) : (
              <div className="scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th className="num">Flights</th>
                      <th className="num">Cancelled</th>
                      <th className="num">Cancel rate</th>
                      <th className="num">Delayed</th>
                      <th className="num">Delay rate</th>
                      <th className="num">Avg delay</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...data].reverse().map((day) => (
                      <tr key={day.stat_date}>
                        <td>{shortDate(day.stat_date)}</td>
                        <td className="num">{count(day.total_flights)}</td>
                        <td className="num">{count(day.cancelled_flights)}</td>
                        <td className="num">{pct(day.cancellation_rate)}</td>
                        <td className="num">{count(day.delayed_flights)}</td>
                        <td className="num">{pct(day.delay_rate)}</td>
                        <td className="num">{minutes(day.avg_delay_minutes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </Guard>
      </Panel>
    </>
  );
}
