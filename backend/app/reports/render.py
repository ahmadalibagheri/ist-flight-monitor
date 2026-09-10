"""Report renderers.

:func:`render_hourly_text` reproduces the layout in section 14 of the
specification verbatim. :func:`render_hourly_telegram` emits the same content in
Telegram MarkdownV2, where a set of ASCII characters must be escaped.

Formatting rule applied throughout: a metric that has no data prints ``n/a``,
never ``0``. "We do not know the average delay" and "the average delay is zero
minutes" are different statements and the report must not conflate them.
"""

from __future__ import annotations

from app.core.config import settings
from app.reports.builder import HistoricalReport, HourlyReport, RouteBlock

_WIDTH = 40
_RULE = "=" * _WIDTH
_THIN = "-" * _WIDTH

#: Characters Telegram MarkdownV2 requires escaping outside code spans.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _minutes(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} min"


def _int(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}/100"


# --------------------------------------------------------------------- hourly
def render_hourly_text(report: HourlyReport) -> str:
    """Plain-text hourly report, per spec section 14."""
    lines: list[str] = [
        _RULE,
        "IST FLIGHT MONITOR",
        f"Generated: {report.generated_at_local}",
        f"Timezone:  {settings.operational_timezone}",
        _RULE,
        "",
    ]

    if report.data_note:
        lines += [report.data_note, "", _THIN, ""]

    for block in report.routes:
        lines += _route_section(block)

    lines += _repeat_section(report)
    lines += _issues_section(report)
    lines += _time_section(report)
    lines += _reliability_section(report)
    lines.append(_RULE)
    return "\n".join(lines)


def _route_section(block: RouteBlock) -> list[str]:
    m = block.metrics
    return [
        f"{block.destination_name} ({block.route.replace('-', ' -> ')})",
        "",
        f"Flights today:     {m.total_flights}",
        f"Departed:          {block.departed}",
        f"Delayed:           {m.delayed_flights}",
        f"Cancelled:         {m.cancelled_flights}",
        f"Still scheduled:   {block.scheduled_remaining}",
        "",
        f"Average delay:     {_minutes(m.avg_delay_minutes)}",
        f"Median delay:      {_minutes(m.median_delay_minutes)}",
        f"Cancellation rate: {_pct(m.cancellation_rate)}",
        f"On-time rate:      {_pct(m.on_time_rate)}",
        "",
        _THIN,
        "",
    ]


def _repeat_section(report: HourlyReport) -> list[str]:
    """Flights that keep getting cancelled.

    Placed before the current-issues block because it is forward-looking: a flight
    cancelled every day it was scheduled is the one thing worth knowing *before*
    booking, and it is absent from the reliability ranking by design (those flights
    fall below the minimum sample size).
    """
    if not report.repeat_cancellations:
        return []

    lines = ["REPEATEDLY CANCELLED", ""]
    for entry in report.repeat_cancellations:
        rate = entry.get("cancellation_rate")
        share = f"{float(rate) * 100:.0f}%" if isinstance(rate, int | float) else "n/a"
        lines.append(
            f"  - {entry['flight_number']} ({entry['airline']}) "
            f"{entry['scheduled_local_time']} -> "
            f"{entry['cancellations']} of {entry['scheduled_occasions']} days ({share})"
        )
    lines += [
        "",
        f"  Observed over the last {report.history_days} days. Shown regardless of",
        "  sample size - a flight that never operates is a finding, not noise.",
        "",
        _THIN,
        "",
    ]
    return lines


def _issues_section(report: HourlyReport) -> list[str]:
    lines = ["CURRENT ISSUES", ""]

    lines.append("Cancelled flights:")
    if report.cancelled:
        for issue in report.cancelled:
            reason = f" - {issue.reason}" if issue.reason else ""
            lines.append(
                f"  - {issue.flight_number} ({issue.airline}) "
                f"{issue.scheduled_local} -> {issue.destination}{reason}"
            )
    else:
        lines.append("  None")
    lines.append("")

    lines.append("Delayed flights:")
    if report.delayed:
        for issue in report.delayed:
            lines.append(
                f"  - {issue.flight_number} ({issue.airline}) "
                f"{issue.scheduled_local} -> {issue.destination}: "
                f"+{_int(issue.delay_minutes)} min"
            )
    else:
        lines.append("  None")
    lines += ["", _THIN, ""]
    return lines


def _time_section(report: HourlyReport) -> list[str]:
    lines = ["TIME ANALYSIS", f"(based on the last {report.history_days} days)", ""]
    for verdict in report.period_verdicts:
        lines.append(verdict.route.replace("-", " -> "))
        if verdict.note:
            lines += [f"  {verdict.note}", ""]
            continue
        lines += [
            f"  Highest delay period:        {verdict.worst_delay_period or 'n/a'}",
            f"  Highest cancellation period: {verdict.worst_cancellation_period or 'n/a'}",
            f"  Best period:                 {verdict.best_period or 'n/a'}",
            "",
        ]
    lines += [_THIN, ""]
    return lines


def _reliability_section(report: HourlyReport) -> list[str]:
    lines = ["RELIABILITY", ""]
    if report.best_flight and report.worst_flight:
        best, worst = report.best_flight, report.worst_flight
        lines += [
            f"Best flight:  {best.key} - {_score(best.score)} "
            f"({best.sample_size} flights)",
            f"Worst flight: {worst.key} - {_score(worst.score)} "
            f"({worst.sample_size} flights)",
        ]
    else:
        lines.append(
            f"Not enough history yet - a flight needs at least "
            f"{settings.min_sample_size_flight} observed departures to be ranked."
        )
    lines.append("")
    return lines


# ----------------------------------------------------------------- historical
def render_historical_text(report: HistoricalReport) -> str:
    """Plain-text rolling-window report, per spec section 15."""
    lines = [
        _RULE,
        f"LAST {report.days} DAYS",
        f"{report.start_date} .. {report.end_date}",
        _RULE,
        "",
    ]
    if report.data_note:
        lines += [report.data_note, "", _RULE]
        return "\n".join(lines)

    for route in report.routes:
        lines += [
            str(route["route"]).replace("-", " -> "),
            f"  Flights:           {route['total_flights']}",
            f"  Cancellation rate: {_pct(route['cancellation_rate'])}",  # type: ignore[arg-type]
            f"  Average delay:     {_minutes(route['avg_delay_minutes'])}",  # type: ignore[arg-type]
            f"  Median delay:      {_minutes(route['median_delay_minutes'])}",  # type: ignore[arg-type]
            f"  On-time rate:      {_pct(route['on_time_rate'])}",  # type: ignore[arg-type]
            f"  Reliability:       {_score(route['reliability_score'])}",  # type: ignore[arg-type]
            "",
            f"  Worst time (delay):        {route.get('worst_delay_period') or 'n/a'}",
            f"  Worst time (cancellation): {route.get('worst_cancellation_period') or 'n/a'}",
            f"  Best time:                 {route.get('best_period') or 'n/a'}",
            "",
            _THIN,
            "",
        ]

    ranked = [a for a in report.airlines if a.get("is_ranked")]
    lines.append("AIRLINE RELIABILITY")
    lines.append(f"(minimum {settings.min_sample_size_airline} flights to be ranked)")
    lines.append("")
    if ranked:
        for airline in ranked:
            lines.append(
                f"  {str(airline['label'])[:22]:<22} "
                f"{_score(airline['reliability_score']):>8}  "  # type: ignore[arg-type]
                f"n={airline['sample_size']}"
            )
    else:
        lines.append("  No airline has reached the minimum sample size yet.")
    lines += ["", _RULE]
    return "\n".join(lines)


# ------------------------------------------------------------------- telegram
def escape_markdown_v2(text: str) -> str:
    """Escape every character Telegram MarkdownV2 treats as markup."""
    return "".join(f"\\{ch}" if ch in _MDV2_SPECIALS else ch for ch in text)


def render_hourly_telegram(report: HourlyReport) -> str:
    """Hourly report as MarkdownV2.

    The body is wrapped in a fenced code block so the fixed-width layout survives,
    which also means only the fence content needs escaping - not every metric.
    """
    body = render_hourly_text(report)
    return f"*IST Flight Monitor*\n```\n{_escape_code_block(body)}\n```"


def render_historical_telegram(report: HistoricalReport) -> str:
    body = render_historical_text(report)
    return f"*IST Flight Monitor - history*\n```\n{_escape_code_block(body)}\n```"


def _escape_code_block(text: str) -> str:
    """Inside a fenced block only backslashes and backticks need escaping."""
    return text.replace("\\", "\\\\").replace("`", "\\`")
