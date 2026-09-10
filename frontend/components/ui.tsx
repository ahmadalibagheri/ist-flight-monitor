"use client";

import type { ReactNode } from "react";
import type { Flight } from "@/lib/api";
import { dayLabel } from "@/lib/format";

/** A missing reading, drawn rather than blanked.
 *
 * The product's central claim is that unknown and zero are different facts. An
 * em dash reads as "nothing here"; a dashed slot reads as "no reading taken",
 * which is what it actually means.
 */
export function Slot({ compact }: { compact?: boolean }) {
  return (
    <span
      className={compact ? "slot compact" : "slot"}
      title="No reading — the provider supplied no usable value. Not the same as zero."
      aria-label="no reading"
    />
  );
}

export function Panel({
  title,
  note,
  legend,
  flush,
  children,
}: {
  title?: string;
  note?: string;
  /** Show the board's reading key. Explained once, not on every row. */
  legend?: boolean;
  flush?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      {(title || note || legend) && (
        <header>
          {title && <h2>{title}</h2>}
          {legend ? <BoardLegend /> : note ? <p className="note">{note}</p> : null}
        </header>
      )}
      <div className={flush ? "body flush" : "body"}>{children}</div>
    </section>
  );
}

export type Tone = "clear" | "caution" | "stop" | undefined;

/** One instrument in the cluster. `value` of null renders a slot, never a zero. */
export function Reading({
  name,
  value,
  qualifier,
  tone,
}: {
  name: string;
  value: string | null;
  qualifier?: string;
  tone?: Tone;
}) {
  return (
    <div className={tone ? `reading ${tone}` : "reading"}>
      <div className="name">{name}</div>
      <div className="figure">{value === null ? <Slot /> : value}</div>
      {qualifier && <div className="qualifier">{qualifier}</div>}
    </div>
  );
}

export function Cluster({ children }: { children: ReactNode }) {
  return <div className="cluster">{children}</div>;
}

export function Message({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "caution" | "stop";
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className={`message ${tone}`}>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}

export function Waiting({ label = "Reading feed" }: { label?: string }) {
  return <div className="waiting">{label}</div>;
}

export function Vacant({ children }: { children: ReactNode }) {
  return <p className="vacant">{children}</p>;
}

export function FeedError({ error }: { error: Error }) {
  const unreachable = "status" in error && (error as { status: number }).status === 0;
  return (
    <Message tone="stop" title={unreachable ? "Feed unreachable" : "Feed error"}>
      {error.message}
      {unreachable && (
        <>
          {" "}
          Start the backend with <code>docker compose up -d</code>, or check{" "}
          <code>NEXT_PUBLIC_API_URL</code>.
        </>
      )}
    </Message>
  );
}

export function Sample({ ranked, size }: { ranked: boolean; size: number }) {
  if (ranked) return <span className="chip">{size} flights</span>;
  return (
    <span className="chip warn" title="Below the minimum sample size, so not ranked.">
      {size} flights, not ranked
    </span>
  );
}

/* --------------------------------------------------------------- the board */

const STATE_TONE: Record<string, string> = {
  SCHEDULED: "is-clear",
  BOARDING: "is-clear",
  DEPARTED: "is-clear",
  ARRIVED: "is-clear",
  DELAYED: "is-caution",
  CANCELLED: "is-stop",
  DIVERTED: "is-stop",
  UNKNOWN: "is-void",
};

const STATE_WORD: Record<string, string> = {
  SCHEDULED: "On schedule",
  BOARDING: "Boarding",
  DEPARTED: "Departed",
  ARRIVED: "Arrived",
  DELAYED: "Delayed",
  CANCELLED: "Cancelled",
  DIVERTED: "Diverted",
  UNKNOWN: "No status",
};

/** Widest delay the bar represents at full width; beyond this it simply caps. */
const BAR_CEILING_MINUTES = 120;

/**
 * One flight, laid out as a strip.
 *
 * Fields sit at fixed positions so a column can be scanned vertically, the way a
 * departure board or an ATC flight strip is read. The leading edge carries
 * status, so the row itself stays quiet.
 */
export function Strip({ flight, showDate }: { flight: Flight; showDate?: boolean }) {
  const tone = STATE_TONE[flight.status] ?? "is-void";
  const clock = flight.scheduled_departure_local?.slice(11) ?? "--:--";
  const late = flight.delay_minutes;
  // A flight the airline pushed later reads as "on time" against its new schedule.
  // Surface the movement so the board cannot flatter it.
  const moved = flight.schedule_moved_minutes ?? 0;
  const retimed = moved >= 15;
  // The provider stopped reporting this flight while it was still mid-transition.
  // Its status is the last thing we were told, not necessarily what happened.
  const stale = flight.data_quality === "STALE";

  return (
    <div className={showDate ? `strip dated ${tone}` : `strip ${tone}`}>
      <span className="edge" aria-hidden />
      <span className="ident">{flight.flight_number}</span>
      <span className="carrier">{flight.airline_name ?? flight.airline_iata ?? "Unknown"}</span>
      {showDate && <span className="day">{dayLabel(flight.flight_date_local)}</span>}
      <time dateTime={flight.scheduled_departure_utc}>{clock}</time>
      <span className="lateness">
        {late === null ? (
          // No caption here: the dashed slot is the vocabulary, and the board's
          // header explains it once. Repeating it on every row drowns the delays.
          <Slot />
        ) : late > 0 ? (
          <>
            <span
              className="bar"
              style={{ width: `${Math.min(100, (late / BAR_CEILING_MINUTES) * 100)}%` }}
            />
            <span className="amount">+{late}</span>
          </>
        ) : (
          <span className="ontime">to time</span>
        )}
        {retimed && (
          <span
            className="retimed"
            title={`Re-timed ${moved} minutes later than first advertised. Delay is measured against the current schedule, so this flight reads as on time.`}
          >
            retimed +{moved >= 120 ? `${Math.round(moved / 60)}h` : `${moved}m`}
          </span>
        )}
      </span>
      <span className="state">
        {stale && (
          <span
            className="lastheard"
            title="No longer reported by the provider. This is the last status we were told, not a confirmed outcome."
          >
            last heard{" "}
          </span>
        )}
        {STATE_WORD[flight.status] ?? flight.status}
        <span className="leg"> · {flight.destination_iata}</span>
      </span>
    </div>
  );
}

export function BoardLegend() {
  return (
    <span className="legend">
      <span>
        <Slot /> no timing reported
      </span>
      <span>bars scale to two hours</span>
    </span>
  );
}

export function Board({
  flights,
  showDate,
}: {
  flights: Flight[];
  /** Show the departure date. Needed whenever the board spans more than one day -
   *  the same flight number recurs daily at the same time and is otherwise
   *  indistinguishable. */
  showDate?: boolean;
}) {
  if (flights.length === 0) {
    return <Vacant>No flights on the board for this window.</Vacant>;
  }
  return (
    <div className="board">
      {flights.map((flight) => (
        <Strip key={flight.id} flight={flight} showDate={showDate} />
      ))}
    </div>
  );
}
