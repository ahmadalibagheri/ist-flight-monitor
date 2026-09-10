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
} from "@/lib/api";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "tehran", label: "Tehran" },
  { id: "mashhad", label: "Mashhad" },
  { id: "airlines", label: "Airlines" },
  { id: "flights", label: "Flights" },
  { id: "delays", label: "Delays" },
  { id: "cancellations", label: "Cancellations" },
  { id: "timeofday", label: "Time of day" },
  { id: "trends", label: "Historical trends" },
] as const;

type TabId = (typeof TABS)[number]["id"];

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
          <h1 className="corridor">
            IST <span className="arrow">→</span> <span className="dest">IKA</span>{" "}
            <span className="arrow">/</span> <span className="dest">MHD</span>
          </h1>
          <p className="strapline">
            Direct departures from Istanbul to Tehran and Mashhad. Times are Istanbul
            local.
          </p>
        </div>
        <div>
          <BoardClock />
          <Feeds />
        </div>
      </header>

      <nav className="sections" role="tablist">
        {TABS.map((entry) => (
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
                {entry.route.replace("-", " → ")}
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
      {tab === "tehran" && (
        <RouteSection filters={filters} route="IST-IKA" title="Tehran" />
      )}
      {tab === "mashhad" && (
        <RouteSection filters={filters} route="IST-MHD" title="Mashhad" />
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
