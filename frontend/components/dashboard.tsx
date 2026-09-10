"use client";

import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import {
  AirlinesSection,
  CancellationsSection,
  DelaysSection,
  FlightsSection,
  OverviewSection,
  RouteSection,
  TimeOfDaySection,
  TrendsSection,
} from "@/components/sections";
import { Message } from "@/components/ui";
import {
  type Airline,
  type Filters,
  type Provider,
  type RouteInfo,
  endpoints,
  fetcher,
  routeLabel,
  routePair,
} from "@/lib/api";

const OVERVIEW_TAB = { id: "overview", label: "Overview" } as const;

/** Tabs that exist regardless of which routes are configured. */
const ANALYSIS_TABS = [
  { id: "airlines", label: "Airlines" },
  { id: "flights", label: "Flights" },
  { id: "delays", label: "Delays" },
  { id: "cancellations", label: "Cancellations" },
  { id: "timeofday", label: "Time of day" },
  { id: "trends", label: "Historical trends" },
] as const;

/**
 * Per-route tabs come from `GET /routes` at runtime, so their ids are namespaced —
 * a route code can then never collide with one of the fixed tab ids.
 */
const ROUTE_TAB = "route:";

type RouteTabId = `${typeof ROUTE_TAB}${string}`;
type TabId = typeof OVERVIEW_TAB.id | (typeof ANALYSIS_TABS)[number]["id"] | RouteTabId;

interface Tab {
  id: TabId;
  label: string;
}

interface Corridor {
  hub: string;
  arrow: string;
  spokes: string[];
  sentence: string;
}

/** "A, B and C" — the list style used in the strapline. */
function joinList(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Describe the corridor the deployment is watching.
 *
 * Routes are directional and may include return legs (IKA-IST alongside IST-IKA),
 * so the hub is whichever airport appears in the most legs — origins weighted, to
 * break the tie on a single unpaired route — and the arrow says which directions
 * are actually collected: outbound, inbound, or both.
 */
function describeCorridor(routes: RouteInfo[]): Corridor | null {
  if (routes.length === 0) return null;

  const seen: string[] = [];
  const weight = new Map<string, number>();
  const names = new Map<string, string>();
  const bump = (code: string, points: number) => {
    if (!seen.includes(code)) seen.push(code);
    weight.set(code, (weight.get(code) ?? 0) + points);
  };
  for (const entry of routes) {
    // An origin counts double so a lone leg resolves to its origin as the hub.
    bump(entry.origin_iata, 3);
    bump(entry.destination_iata, 2);
    // Cities only: `*_name` is the airport's own name, which reads badly in prose.
    if (entry.origin_city) names.set(entry.origin_iata, entry.origin_city);
    if (entry.destination_city) names.set(entry.destination_iata, entry.destination_city);
  }

  const hub = seen.reduce((best, code) =>
    (weight.get(code) ?? 0) > (weight.get(best) ?? 0) ? code : best,
  );
  const outbound = routes.some((entry) => entry.origin_iata === hub);
  const inbound = routes.some((entry) => entry.destination_iata === hub);
  const spokes = seen.filter((code) => code !== hub);

  const hubName = names.get(hub) ?? hub;
  const spokeNames = joinList(spokes.map((code) => names.get(code) ?? code));
  const sentence =
    spokes.length === 0
      ? `Flights at ${hubName}.`
      : outbound && inbound
        ? `Direct flights linking ${hubName} with ${spokeNames}.`
        : inbound
          ? `Direct arrivals into ${hubName} from ${spokeNames}.`
          : `Direct departures from ${hubName} to ${spokeNames}.`;

  return {
    hub,
    arrow: outbound && inbound ? "⇄" : inbound ? "←" : "→",
    spokes,
    sentence,
  };
}

const WINDOWS = [
  { value: "today", label: "Today" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "mtd", label: "Month to date" },
  { value: "all", label: "All time" },
];

/** Banner describing what the deployment is actually collecting from. */
function ProviderBanner() {
  const { data } = useSWR<Provider[], Error>(endpoints.providers(), fetcher<Provider[]>, {
    refreshInterval: 120_000,
    revalidateOnFocus: false,
  });
  if (!data) return null;

  const active = data.filter((p) => p.in_chain && p.configured && p.kind === "REAL");
  const mockActive = data.some((p) => p.in_chain && p.kind === "MOCK" && p.configured);

  return (
    <>
      {active.length === 0 && (
        <Message tone="stop" title="No real data provider is configured">
          Collection cannot run. Set an API key and add the provider to{" "}
          <code>PROVIDER_CHAIN</code> — see <code>docs/PROVIDERS.md</code>.
        </Message>
      )}
      {mockActive && (
        <Message tone="caution" title="Mock provider is enabled">
          Synthetic flights are being generated for development. They are tagged{" "}
          <code>is_mock</code> and excluded from every statistic on this dashboard.
        </Message>
      )}
    </>
  );
}

function BoardClock() {
  const [now, setNow] = useState<string>("");
  useEffect(() => {
    const tick = () =>
      setNow(
        new Intl.DateTimeFormat("en-GB", {
          hour: "2-digit",
          minute: "2-digit",
          timeZone: "Europe/Istanbul",
          hour12: false,
        }).format(new Date()),
      );
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="board-clock">
      <div className="time">{now || "--:--"}</div>
      <div className="zone">Istanbul</div>
    </div>
  );
}

function Feeds() {
  const { data } = useSWR<Provider[], Error>(endpoints.providers(), fetcher<Provider[]>, {
    refreshInterval: 120_000,
    revalidateOnFocus: false,
  });
  const chain = data?.filter((p) => p.in_chain) ?? [];
  if (chain.length === 0) return <div className="feeds" />;

  return (
    <div className="feeds">
      {chain.map((provider) => {
        const state = !provider.configured
          ? "down"
          : provider.is_healthy === false
            ? "degraded"
            : "live";
        const word =
          state === "down" ? "no key" : state === "degraded" ? "degraded" : "live";
        return (
          <span key={provider.name} className={`feed ${state}`} title={provider.description}>
            <span className="lamp" />
            {provider.name} {word}
          </span>
        );
      })}
    </div>
  );
}

export default function Dashboard() {
  const [tab, setTab] = useState<TabId>("overview");
  const [window, setWindow] = useState("30d");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [route, setRoute] = useState("");
  const [airline, setAirline] = useState("");
  const [flightNumber, setFlightNumber] = useState("");

  const { data: routes } = useSWR<RouteInfo[], Error>(endpoints.routes(), fetcher<RouteInfo[]>);
  const { data: airlines } = useSWR<Airline[], Error>(
    endpoints.airlineList(),
    fetcher<Airline[]>,
  );

  const routeTabs: Tab[] = useMemo(
    () =>
      (routes ?? []).map((entry) => ({
        id: `${ROUTE_TAB}${entry.route}` as RouteTabId,
        label: routeLabel(entry),
      })),
    [routes],
  );
  const tabs: Tab[] = [OVERVIEW_TAB, ...routeTabs, ...ANALYSIS_TABS];
  const routeTab = routeTabs.find((entry) => entry.id === tab);
  const corridor = useMemo(() => describeCorridor(routes ?? []), [routes]);

  // A route can stop being collected between page loads; don't strand the user on a
  // tab that no longer has a route behind it.
  useEffect(() => {
    if (!routes || !tab.startsWith(ROUTE_TAB)) return;
    if (!routes.some((entry) => `${ROUTE_TAB}${entry.route}` === tab)) setTab("overview");
  }, [routes, tab]);

  const filters: Filters = useMemo(
    () => ({
      window,
      startDate: startDate && endDate ? startDate : undefined,
      endDate: startDate && endDate ? endDate : undefined,
      route: route || undefined,
      airline: airline || undefined,
      flightNumber: flightNumber || undefined,
    }),
    [window, startDate, endDate, route, airline, flightNumber],
  );

  function reset() {
    setWindow("30d");
    setStartDate("");
    setEndDate("");
    setRoute("");
    setAirline("");
    setFlightNumber("");
  }

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          {/*
            The corridor is unknown until /routes answers. Rather than flash a
            hardcoded pair, the headline holds its space and the strapline shows only
            the half that is true for every deployment.
          */}
          <h1 className="corridor">
            {corridor ? (
              <>
                {corridor.hub} <span className="arrow">{corridor.arrow}</span>{" "}
                {corridor.spokes.flatMap((code, index) => [
                  index > 0 ? (
                    <span key={`sep-${code}`} className="arrow">
                      {" / "}
                    </span>
                  ) : null,
                  <span key={code} className="dest">
                    {code}
                  </span>,
                ])}
              </>
            ) : (
              " "
            )}
          </h1>
          <p className="strapline">
            {corridor ? `${corridor.sentence} ` : ""}
            Times are shown in the monitor&apos;s operational timezone.
          </p>
        </div>
        <div>
          <BoardClock />
          <Feeds />
        </div>
      </header>

      <nav className="sections" role="tablist">
        {tabs.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            type="button"
            aria-selected={tab === entry.id}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="filters">
        <div className="f">
          <label htmlFor="window">Period</label>
          <select
            id="window"
            value={window}
            onChange={(event) => {
              setWindow(event.target.value);
              setStartDate("");
              setEndDate("");
            }}
          >
            {WINDOWS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
        <div className="f">
          <label htmlFor="start">From</label>
          <input
            id="start"
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
          />
        </div>
        <div className="f">
          <label htmlFor="end">To</label>
          <input
            id="end"
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
          />
        </div>
        <div className="f">
          <label htmlFor="route">Route</label>
          <select id="route" value={route} onChange={(event) => setRoute(event.target.value)}>
            <option value="">All routes</option>
            {(routes ?? []).map((entry) => (
              <option key={entry.route} value={entry.route}>
                {routePair(entry.route)}
              </option>
            ))}
          </select>
        </div>
        <div className="f">
          <label htmlFor="airline">Airline</label>
          <select
            id="airline"
            value={airline}
            onChange={(event) => setAirline(event.target.value)}
          >
            <option value="">All airlines</option>
            {(airlines ?? [])
              .filter((entry) => entry.iata)
              .map((entry) => (
                <option key={entry.iata} value={entry.iata as string}>
                  {entry.name} ({entry.iata})
                </option>
              ))}
          </select>
        </div>
        <div className="f">
          <label htmlFor="flight">Flight number</label>
          <input
            id="flight"
            placeholder="e.g. TK878"
            value={flightNumber}
            onChange={(event) => setFlightNumber(event.target.value.toUpperCase())}
          />
        </div>
        <button type="button" className="linkish" onClick={reset}>
          Clear filters
        </button>
      </div>

      <ProviderBanner />

      {tab === "overview" && <OverviewSection filters={filters} />}
      {routeTab && (
        <RouteSection
          filters={filters}
          route={routeTab.id.slice(ROUTE_TAB.length)}
          title={routeTab.label}
        />
      )}
      {tab === "airlines" && <AirlinesSection filters={filters} />}
      {tab === "flights" && <FlightsSection filters={filters} />}
      {tab === "delays" && <DelaysSection filters={filters} />}
      {tab === "cancellations" && <CancellationsSection filters={filters} />}
      {tab === "timeofday" && <TimeOfDaySection filters={filters} />}
      {tab === "trends" && <TrendsSection filters={filters} />}
    </div>
  );
}
