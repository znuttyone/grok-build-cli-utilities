"""grok-utils usage - gorgeous analytics for Grok Build power users."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.progress import Progress
from rich.table import Table

from ..utils.common import (
    SessionSummary,
    console,
    estimate_cost,
    format_age,
    get_grok_home,
    get_sessions_dir,
    iter_sessions,
    make_table,
    warn,
)
from ..utils.pricing import (
    COST_CAVEATS_LONG,
    COST_CAVEATS_SHORT,
    DEFAULT_CASH_SCALE,
    DEFAULT_RATES_MODEL,
    PlanAdvisorResult,
    TokenRates,
    apply_cash_scale,
    effective_rates,
    list_rate_profiles,
    load_plan_advisor_config,
    load_usage_config,
    plan_advisor,
    resolve_cash_scale,
    resolve_rates_model,
)
from ..utils.usage_tokens import (
    UsageBucket,
    UsageRec,
    aggregate,
    allocate_invoice,
    filter_usage,
    list_price_usd,
    load_turn_usage,
    parse_iso_date,
    total_bucket,
)

app = typer.Typer(help="Usage reports, leaderboards and trends", no_args_is_help=True)


def _sparkline(values: list[int], width: int = 20) -> str:
    """Simple unicode sparkline."""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    mx = max(values) or 1
    scaled = [int((v / mx) * (len(blocks) - 1)) for v in values]
    return "".join(blocks[min(s, len(blocks) - 1)] for s in scaled[-width:])


def _ascii_bar(value: float, maxv: float, width: int = 24) -> str:
    if maxv <= 0:
        return ""
    filled = int((value / maxv) * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _data_date_span(records: list[UsageRec]) -> tuple[date | None, date | None]:
    """Earliest and latest turn dates in records (local calendar date)."""
    if not records:
        return None, None
    days = [r.ts.date() for r in records]
    return min(days), max(days)


def _warn_stderr(msg: str) -> None:
    """Warn on stderr so --json stdout stays pure."""
    import sys

    from rich.console import Console as RichConsole

    RichConsole(file=sys.stderr).print(f"[yellow]⚠[/yellow] {msg}")


def _load_filtered_usage(
    grok_home: Path,
    *,
    since: str | None,
    date_from: str | None,
    date_to: str | None,
    apps: list[str] | None,
) -> tuple[list[UsageRec], date | None, date | None, date | None, date | None]:
    """Load turns, apply filters, warn if CLI range extends past available data.

    Returns (filtered, d_from, d_to, data_earliest, data_latest) where data_* are
    the min/max dates in session logs (app-filtered, before date window).
    """
    import sys

    from rich.console import Console as RichConsole

    sessions_dir = get_sessions_dir(grok_home)
    # Progress on stderr so --json stdout stays pure
    with Progress(console=RichConsole(file=sys.stderr), transient=True) as progress:
        records = load_turn_usage(sessions_dir, progress=progress)

    d_from = None
    d_to = None
    if date_from:
        try:
            d_from = parse_iso_date(date_from)
        except ValueError:
            warn(f"Ignoring bad --from {date_from}")
    if date_to:
        try:
            d_to = parse_iso_date(date_to)
        except ValueError:
            warn(f"Ignoring bad --to {date_to}")
    if since and d_from is None:
        try:
            d_from = parse_iso_date(since)
        except ValueError:
            warn(f"Ignoring bad --since {since}")

    # Universe for span checks: app filter only (ignore date window)
    base = filter_usage(records, apps=apps) if apps else records
    earliest, latest = _data_date_span(base)

    if d_from is not None and earliest is not None and d_from < earliest:
        _warn_stderr(
            f"--from {d_from.isoformat()} is before earliest session data "
            f"({earliest.isoformat()}). No turns exist before {earliest.isoformat()}; "
            f"report starts from that date."
        )
    if d_to is not None and latest is not None and d_to > latest:
        _warn_stderr(
            f"--to {d_to.isoformat()} is after latest session data "
            f"({latest.isoformat()}). Report ends at {latest.isoformat()}."
        )
    if d_from is not None and latest is not None and d_from > latest:
        _warn_stderr(
            f"--from {d_from.isoformat()} is after latest session data "
            f"({latest.isoformat()}). No turns match this window."
        )
    if d_to is not None and earliest is not None and d_to < earliest:
        _warn_stderr(
            f"--to {d_to.isoformat()} is before earliest session data "
            f"({earliest.isoformat()}). No turns match this window."
        )

    filtered = filter_usage(records, date_from=d_from, date_to=d_to, apps=apps)
    return filtered, d_from, d_to, earliest, latest


@app.command("report")
def report(
    ctx: typer.Context,
    since: str | None = typer.Option(None, "--since", help="Alias for --from"),
    date_from: str | None = typer.Option(
        None, "--from", help="Inclusive start YYYY-MM-DD (omit --to for through latest)"
    ),
    date_to: str | None = typer.Option(
        None, "--to", help="Inclusive end YYYY-MM-DD (omit for through latest session data)"
    ),
    by: str = typer.Option("project", "--by", help="Group by: project | app | model | day"),
    top: int = typer.Option(10, "--top", help="Show top N"),
    tokens: bool = typer.Option(
        False,
        "--tokens",
        help="Use turn-level tokens + list$/est$ (recommended for cost awareness)",
    ),
    rates_model: str = typer.Option(
        DEFAULT_RATES_MODEL,
        "--rates-model",
        "-m",
        help=(
            f"List-rate profile for list$ (default: {DEFAULT_RATES_MODEL}). "
            f"Choices: {', '.join(list_rate_profiles())}"
        ),
    ),
    cash_scale: float | None = typer.Option(
        None,
        "--cash-scale",
        metavar="SCALE",
        help=(
            f"Requires number: scale list$ → est$ (e.g. --cash-scale 0.57). "
            f"Default {DEFAULT_CASH_SCALE}; or usage.cash_scale in ~/.grok/grok-utils.toml"
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Flag (no value): machine-readable JSON"),
) -> None:
    """Generate a rich usage report.

    Token path (--tokens or --by app): list$ + est$ (same cash_scale as usage cost).
    Value options need an argument (e.g. --cash-scale 0.57, --from 2026-08-01).
    """
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    rates_label, rates = resolve_rates_model(rates_model)

    if tokens or by in ("app",):
        # Token path when requested or when grouping by short app name
        use_tokens = tokens or by == "app"
    else:
        use_tokens = tokens

    if use_tokens:
        group = by if by in ("app", "project", "model", "day") else "app"
        records, d_from, d_to, _data_earliest, _data_latest = _load_filtered_usage(
            grok_home, since=since, date_from=date_from, date_to=date_to, apps=None
        )
        if not records:
            warn("No turn usage data for report (try without --tokens for summary-based report).")
            return
        buckets = aggregate(records, group)
        tot = total_bucket(records)
        list_total = tot.api_est(rates)
        result_earliest, result_latest = _data_date_span(records)

        usage_cfg = load_usage_config(grok_home)
        cfg_scale = usage_cfg.get("cash_scale")
        if cfg_scale is not None:
            try:
                cfg_scale = float(cfg_scale)
            except (TypeError, ValueError):
                cfg_scale = None
        cash_scale_val, cash_scale_src = resolve_cash_scale(
            cli_scale=cash_scale,
            list_total_usd=list_total,
            config_scale=cfg_scale,
        )
        est_total = apply_cash_scale(list_total, cash_scale_val)

        # Sort by spend-oriented est$ (same as usage cost)
        buckets.sort(key=lambda b: -apply_cash_scale(b.api_est(rates), cash_scale_val))

        if json_out:
            import json

            top_rows = []
            for b in buckets[:top]:
                list_b = b.api_est(rates)
                row = b.to_dict(rates)
                row["list_usd"] = round(list_b, 4)
                row["api_est_usd"] = round(list_b, 4)  # alias
                row["est_usd"] = round(apply_cash_scale(list_b, cash_scale_val), 4)
                top_rows.append(row)

            print(
                json.dumps(
                    {
                        "mode": "tokens",
                        "by": group,
                        "from": d_from.isoformat() if d_from else None,
                        "to": d_to.isoformat() if d_to else None,
                        "result_from": (
                            result_earliest.isoformat() if result_earliest else None
                        ),
                        "result_to": (
                            result_latest.isoformat() if result_latest else None
                        ),
                        "rates_model": rates_label,
                        "rates": rates.as_dict(),
                        "cash_scale": cash_scale_val,
                        "cash_scale_source": cash_scale_src,
                        "totals": {
                            **tot.to_dict(rates),
                            "list_usd": round(list_total, 4),
                            "api_est_usd": round(list_total, 4),
                            "est_usd": round(est_total, 4),
                        },
                        "buckets": top_rows,
                        "caveats": [
                            "list$_is_pure_api_list_rates",
                            "est$_is_list_times_cash_scale_spend_oriented",
                        ],
                    },
                    indent=2,
                )
            )
            return

        # Title period = actual dates in the filtered session data
        period = ""
        if result_earliest or result_latest:
            left = result_earliest.isoformat() if result_earliest else "…"
            right = result_latest.isoformat() if result_latest else "…"
            period = f" · {left} → {right}"
            if d_from is not None and result_earliest is not None and d_from < result_earliest:
                period += f"  (requested --from {d_from.isoformat()})"
        title = (
            f"Usage by {group} (token-based, top {top}, {tot.n} prompts, "
            f"{_fmt_tokens(tot.total)} tok, list$ ${list_total:.2f}, "
            f"est$ ${est_total:.2f}){period}"
        )
        t = make_table(
            title,
            ["Key", "Prm", "Tokens", "Cache%", "list$", "est$", "Share(est$)", "Share(tok)"],
        )
        max_est = max(
            (apply_cash_scale(b.api_est(rates), cash_scale_val) for b in buckets[:top]),
            default=1.0,
        ) or 1.0
        max_tok = max((b.total for b in buckets[:top]), default=1) or 1
        for b in buckets[:top]:
            list_b = b.api_est(rates)
            est_b = apply_cash_scale(list_b, cash_scale_val)
            key = b.key[:44] + ("…" if len(b.key) > 44 else "")
            t.add_row(
                key,
                str(b.n),
                _fmt_tokens(b.total),
                f"{b.cache_pct:.1f}%",
                f"{list_b:.2f}",
                f"{est_b:.2f}",
                _ascii_bar(est_b, max_est, 12),
                _ascii_bar(float(b.total), float(max_tok), 12),
            )
        console.print(t)
        console.print(f"\n[bold]Rates model (list$):[/bold] {rates.short_label()}")
        console.print(
            f"[bold]Cash scale (est$):[/bold] {cash_scale_val:.4g}  "
            f"[dim]({cash_scale_src})[/dim]"
        )
        console.print(
            f"[bold]TOTALS[/bold]  prompts={tot.n:,}  tokens={tot.total:,}  "
            f"cache={tot.cache_pct:.1f}%  "
            f"list$=${list_total:.2f}  "
            f"[bold green]est$=${est_total:.2f}[/bold green]"
        )
        # spark of daily tokens
        day_buckets = aggregate(records, "day")
        vals = [b.total for b in day_buckets[-14:]]
        if vals:
            console.print(
                f"[bold]Daily tokens spark (last {len(vals)} days):[/bold] "
                f"{_sparkline(vals)}  (max {_fmt_tokens(max(vals))})"
            )
        console.print()
        console.print(COST_CAVEATS_SHORT, style="dim", markup=False)
        return

    # Legacy summary.json path
    sessions = list(iter_sessions(grok_home))

    if since or date_from:
        raw = date_from or since
        try:
            cutoff = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)  # type: ignore[arg-type]
            sessions = [
                s
                for s in sessions
                if (s.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
            ]
        except (ValueError, TypeError, OverflowError):
            warn(f"Ignoring bad date filter {raw}")

    if not sessions:
        warn("No data for report.")
        return

    group_by = by if by in ("project", "model", "day") else "project"
    groups: defaultdict[str, list[SessionSummary]] = defaultdict(list)
    for s in sessions:
        key = {
            "project": s.cwd,
            "model": s.current_model_id,
            "day": (s.created_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
        }[group_by]
        groups[key].append(s)

    rows = []
    for k, ss in groups.items():
        msgs = sum(x.num_messages for x in ss)
        actives: list[datetime] = []
        for x in ss:
            dt = x.last_active_at or x.created_at
            if dt:
                actives.append(dt)
        last = max(actives) if actives else None
        rows.append((k, len(ss), msgs, last))

    rows.sort(key=lambda r: (-r[1], -r[2]))

    if json_out:
        import json

        print(
            json.dumps(
                [
                    {
                        "key": r[0],
                        "sessions": r[1],
                        "messages": r[2],
                        "last": r[3].isoformat() if r[3] else None,
                    }
                    for r in rows[:top]
                ],
                indent=2,
            )
        )
        return

    title = f"Usage by {group_by} (top {top}, {len(sessions)} total sessions)"
    t = make_table(title, ["Key", "Sessions", "Messages", "Last Active", "Share"])
    max_sess = max(r[1] for r in rows) or 1
    for k, nsess, nmsg, last in rows[:top]:
        share = _ascii_bar(nsess, max_sess, 18)
        t.add_row(
            k[:48] + ("…" if len(k) > 48 else ""),
            str(nsess),
            str(nmsg),
            format_age(last),
            share,
        )
    console.print(t)
    console.print(
        "\n[dim]Tip: grok-utils usage report --tokens --by app  for token-accurate + api$ bars[/dim]"
    )

    days: defaultdict[str, int] = defaultdict(int)
    for s in sessions:
        d = (s.created_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        days[d] += 1
    recent = sorted(days.items())[-14:]
    vals = [v for _, v in recent]
    if vals:
        console.print(
            f"\n[bold]Recent activity spark (last {len(vals)} days):[/bold] {_sparkline(vals)}  (max {max(vals)})"
        )


@app.command("top-projects")
def top_projects(ctx: typer.Context, n: int = typer.Option(8, "--count", "-n")) -> None:
    """Leaderboard of projects by session count and activity."""
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    sessions = list(iter_sessions(grok_home))

    proj: defaultdict[str, dict] = defaultdict(lambda: {"count": 0, "msgs": 0, "last": None})
    for s in sessions:
        p = proj[s.cwd]
        p["count"] += 1
        p["msgs"] += s.num_messages
        act = s.last_active_at or s.created_at
        if act and (not p["last"] or act > p["last"]):
            p["last"] = act

    ranked = sorted(proj.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["msgs"]))[:n]

    t = make_table("Top Projects", ["Project", "Sessions", "Messages", "Last Used"])
    for path, data in ranked:
        short = path if len(path) < 50 else "…" + path[-49:]
        t.add_row(short, str(data["count"]), str(data["msgs"]), format_age(data["last"]))
    console.print(t)


@app.command("models")
def models_usage(ctx: typer.Context) -> None:
    """Model distribution and preferences."""
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    sessions = list(iter_sessions(grok_home))

    counts: defaultdict[str, int] = defaultdict(int)
    for s in sessions:
        counts[s.current_model_id] += 1

    if not counts:
        return

    t = make_table("Model Usage", ["Model", "Sessions", "Share"])
    mx = max(counts.values())
    for m, c in sorted(counts.items(), key=lambda x: -x[1]):
        bar = _ascii_bar(c, mx, 30)
        pct = f"{100 * c / len(sessions):.1f}%"
        t.add_row(m, f"{c} ({pct})", bar)
    console.print(t)


@app.command("timeline")
def timeline(ctx: typer.Context, days: int = typer.Option(30, "--days", "-d")) -> None:
    """Daily activity over the last N days."""
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    sessions = list(iter_sessions(grok_home))

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    buckets: defaultdict[str, int] = defaultdict(int)
    for s in sessions:
        if s.created_at and s.created_at >= cutoff:
            key = s.created_at.strftime("%m-%d")
            buckets[key] += 1

    if not buckets:
        warn("No recent activity.")
        return

    t = Table(title=f"Daily Sessions (last {days}d)", show_header=False, box=None)
    maxv = max(buckets.values()) or 1
    for day in sorted(buckets.keys()):
        bar = _ascii_bar(buckets[day], maxv, 28)
        t.add_row(day, bar, str(buckets[day]))
    console.print(t)


@app.command("info")
def cost_info() -> None:
    """FAQ: why list$/est$ differ from Build Session Cost, Credits, weekly limit."""
    # markup=False so toml [usage] brackets are not treated as Rich tags
    console.print(COST_CAVEATS_LONG, markup=False)


@app.command("cost")
def cost_report(
    ctx: typer.Context,
    since: str | None = typer.Option(
        None, "--since", metavar="DATE", help="Alias for --from (YYYY-MM-DD)"
    ),
    date_from: str | None = typer.Option(
        None,
        "--from",
        metavar="DATE",
        help="Inclusive start YYYY-MM-DD (omit --to for through latest)",
    ),
    date_to: str | None = typer.Option(
        None,
        "--to",
        metavar="DATE",
        help="Inclusive end YYYY-MM-DD (omit for through latest session data)",
    ),
    by: str = typer.Option(
        "app",
        "--by",
        metavar="KEY",
        help="Group cost by: app | project | model | day | week | month",
    ),
    top: int = typer.Option(8, "--top", metavar="N", help="Show top N buckets"),
    mode: str = typer.Option(
        "tokens",
        "--mode",
        metavar="MODE",
        help="tokens (default, turn usage) | rough (legacy message×400)",
    ),
    invoice_usd: float | None = typer.Option(
        None,
        "--invoice-usd",
        metavar="USD",
        help=(
            "Requires amount: total SuperGrok/cash $ to allocate by costUsdTicks "
            "(e.g. --invoice-usd 180). Optional companion: --fixed-usd 30"
        ),
    ),
    fixed_usd: float = typer.Option(
        0.0,
        "--fixed-usd",
        metavar="USD",
        help="Requires amount with --invoice-usd: fixed fee amortized across buckets (e.g. 30)",
    ),
    rates_model: str = typer.Option(
        DEFAULT_RATES_MODEL,
        "--rates-model",
        "-m",
        metavar="MODEL",
        help=(
            f"List-rate profile for list$ (default: {DEFAULT_RATES_MODEL}). "
            f"Choices: {', '.join(list_rate_profiles())}"
        ),
    ),
    cash_scale: float | None = typer.Option(
        None,
        "--cash-scale",
        metavar="SCALE",
        help=(
            f"Requires number: scale list$ → est$ (e.g. --cash-scale 0.57). "
            f"Default {DEFAULT_CASH_SCALE}; or set usage.cash_scale in ~/.grok/grok-utils.toml"
        ),
    ),
    prepaid_usd: float | None = typer.Option(
        None,
        "--prepaid-usd",
        metavar="USD",
        help=(
            "Requires amount: prepaid loaded for window (e.g. --prepaid-usd 60). "
            "Use together with --credits-remaining"
        ),
    ),
    credits_remaining: float | None = typer.Option(
        None,
        "--credits-remaining",
        metavar="USD",
        help=(
            "Requires amount: credits left in Build UI (e.g. --credits-remaining 28.12). "
            "Use together with --prepaid-usd → burn/list scale"
        ),
    ),
    list_price: bool = typer.Option(
        False,
        "--list-price",
        help="Flag (no value): show costUsdTicks/1e9 (usually overstates cash)",
    ),
    api_estimate: bool = typer.Option(
        False,
        "--api-estimate",
        help="Flag (no value): print pure-API list-rate breakdown for filtered total",
    ),
    plan_advisor_flag: bool = typer.Option(
        False,
        "--plan-advisor",
        "-P",
        help=(
            "Flag (no value): compare pure API vs SuperGrok vs SuperGrok Heavy "
            "for this window (run-rate + monthly projection). "
            "Weekly pool sizes are estimates."
        ),
    ),
    app: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--app",
        metavar="NAME",
        help="Requires substring: filter by app/project (repeatable, e.g. --app VCI)",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Flag (no value): machine-readable JSON"
    ),
) -> None:
    """Token-accurate cost: list$ (list rates) + est$ (spend-oriented, scaled).

    Options that take a value need the number/date on the same flag
    (e.g. --invoice-usd 180, not bare --invoice-usd). Use: grok-utils usage cost --help

    est$ = list$ × cash_scale (~prepaid burn). Day-to-day scale from
    ~/.grok/grok-utils.toml  (section usage, key cash_scale), else built-in default.

      # Closed window
      grok-utils usage cost --from 2026-08-01 --to 2026-08-05 --by app -m grok-4.5

      # From a date through latest session data (omit --to)
      grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5

      # Plan advisor: API vs SuperGrok vs Heavy for the window
      grok-utils usage cost --from 2026-07-18 --by app -m grok-4.5 --plan-advisor

      # One-shot wallet fit for a window (both amounts required)
      grok-utils usage cost ... --prepaid-usd 60 --credits-remaining 12.12

      # Invoice allocation (amount required)
      grok-utils usage cost ... --invoice-usd 180 --fixed-usd 30

    See: grok-utils usage info
    """
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    rates_label, rates = resolve_rates_model(rates_model)
    usage_cfg = load_usage_config(grok_home)
    cfg_scale = usage_cfg.get("cash_scale")
    if cfg_scale is not None:
        try:
            cfg_scale = float(cfg_scale)
        except (TypeError, ValueError):
            cfg_scale = None


    if mode == "rough":
        _cost_rough(ctx, since=since or date_from, by=by if by in ("model", "project") else "model", top=top, json_out=json_out)
        return

    if by not in ("app", "project", "model", "day", "week", "month", "none"):
        warn(f"Unknown --by {by}; using app")
        by = "app"

    records, d_from, d_to, data_earliest, data_latest = _load_filtered_usage(
        grok_home, since=since, date_from=date_from, date_to=date_to, apps=app
    )

    if not records:
        warn("No turn usage records (no turn_completed usage in updates.jsonl).")
        warn("Tip: try --mode rough for legacy summary-based estimate, or check --from/--to.")
        return

    buckets = aggregate(records, by)
    tot = total_bucket(records)
    list_total = tot.api_est(rates)
    result_earliest, result_latest = _data_date_span(records)

    cash_scale_val, cash_scale_src = resolve_cash_scale(
        cli_scale=cash_scale,
        prepaid_usd=prepaid_usd,
        credits_remaining=credits_remaining,
        list_total_usd=list_total,
        config_scale=cfg_scale,
    )
    est_total = apply_cash_scale(list_total, cash_scale_val)

    advisor: PlanAdvisorResult | None = None
    if plan_advisor_flag:
        if result_earliest is None or result_latest is None:
            warn("Plan advisor needs dated turns; skipping.")
        else:
            window_days = (result_latest - result_earliest).days + 1
            pa_cfg = load_plan_advisor_config(usage_cfg)
            advisor = plan_advisor(
                list_usd=list_total,
                est_usd=est_total,
                tokens=tot.total,
                cache_pct=tot.cache_pct,
                cash_scale=cash_scale_val,
                window_days=window_days,
                project_days=int(pa_cfg["project_days"]),
                supergrok_usd=pa_cfg["supergrok_usd"],
                heavy_usd=pa_cfg["heavy_usd"],
                supergrok_weekly_include_usd=pa_cfg["supergrok_weekly_include_usd"],
                heavy_weekly_include_usd=pa_cfg["heavy_weekly_include_usd"],
            )

    inv_scale = 0.0
    fixed_per = 0.0
    show_invoice = invoice_usd is not None
    if show_invoice:
        if tot.ticks <= 0:
            warn("No costUsdTicks in data; cannot allocate --invoice-usd.")
            show_invoice = False
        else:
            inv_scale, fixed_per = allocate_invoice(
                buckets,
                invoice_usd=float(invoice_usd),  # type: ignore[arg-type]
                fixed_usd=fixed_usd,
                group=by,
                date_from=d_from,
                date_to=d_to,
            )

    # Sort by spend-oriented est$ (list$ × scale)
    buckets.sort(key=lambda b: -apply_cash_scale(b.api_est(rates), cash_scale_val))

    if json_out:
        import json

        top_rows = []
        for b in buckets[:top]:
            list_b = b.api_est(rates)
            row = b.to_dict(rates)
            row["list_usd"] = round(list_b, 4)
            row["est_usd"] = round(apply_cash_scale(list_b, cash_scale_val), 4)
            if show_invoice:
                var = b.ticks * inv_scale
                row["variable_usd"] = round(var, 4)
                row["total_usd"] = round(var + fixed_per, 4)
            if list_price:
                row["list_price_usd"] = round(list_price_usd(b.ticks), 4)
            top_rows.append(row)

        payload = {
            "mode": "tokens",
            "by": by,
            "from": d_from.isoformat() if d_from else None,
            "to": d_to.isoformat() if d_to else None,
            "data_earliest": data_earliest.isoformat() if data_earliest else None,
            "data_latest": data_latest.isoformat() if data_latest else None,
            "result_from": result_earliest.isoformat() if result_earliest else None,
            "result_to": result_latest.isoformat() if result_latest else None,
            "rates_model": rates_label,
            "rates": rates.as_dict(),
            "cash_scale": cash_scale_val,
            "cash_scale_source": cash_scale_src,
            "effective_rates": effective_rates(rates, cash_scale_val).as_dict(),
            "totals": {
                **tot.to_dict(rates),
                "list_usd": round(list_total, 4),
                "api_est_usd": round(list_total, 4),  # alias
                "est_usd": round(est_total, 4),
                "list_price_usd": round(list_price_usd(tot.ticks), 4) if list_price else None,
            },
            "invoice": (
                {
                    "invoice_usd": invoice_usd,
                    "fixed_usd": fixed_usd,
                    "scale_per_tick": inv_scale,
                    "fixed_per_bucket": fixed_per,
                }
                if show_invoice
                else None
            ),
            "buckets": top_rows,
            "plan_advisor": advisor.as_dict() if advisor else None,
            "caveats": [
                "list$_is_pure_api_list_rates",
                "est$_is_list_times_cash_scale_spend_oriented",
                "effective_rates_are_list_times_uniform_scale",
                "long_context_tier_not_applied",
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    # Human table — primary path: list$ + est$
    # Title uses actual result span; note requested --from when it was before data.
    period = ""
    if result_earliest or result_latest or d_from or d_to:
        left = (
            result_earliest.isoformat()
            if result_earliest
            else (d_from.isoformat() if d_from else "…")
        )
        right = (
            result_latest.isoformat()
            if result_latest
            else (d_to.isoformat() if d_to else "…")
        )
        period = f" · {left} → {right}"
        if d_from is not None and result_earliest is not None and d_from < result_earliest:
            period += f"  (requested --from {d_from.isoformat()})"
    headers = ["Key", "Prm", "Tokens", "Cache%", "list$", "est$"]
    if show_invoice:
        headers += ["var$", "tot$"]
    if list_price:
        headers.append("ticks$")
    headers.append("Share")

    t = make_table(
        f"Estimated Cost by {by} (est$=list$×scale){period}",
        headers,
    )
    share_vals = [apply_cash_scale(b.api_est(rates), cash_scale_val) for b in buckets[:top]]
    max_share = max(share_vals, default=1.0) or 1.0
    for b in buckets[:top]:
        list_b = b.api_est(rates)
        est_b = apply_cash_scale(list_b, cash_scale_val)
        key = b.key[:40] + ("…" if len(b.key) > 40 else "")
        cells: list[str] = [
            key,
            str(b.n),
            _fmt_tokens(b.total),
            f"{b.cache_pct:.1f}%",
            f"{list_b:.2f}",
            f"{est_b:.2f}",
        ]
        if show_invoice:
            var = b.ticks * inv_scale
            cells.extend([f"{var:.2f}", f"{var + fixed_per:.2f}"])
        if list_price:
            cells.append(f"{list_price_usd(b.ticks):.2f}")
        cells.append(_ascii_bar(est_b, max_share, 12))
        t.add_row(*cells)
    console.print(t)

    eff = effective_rates(rates, cash_scale_val)
    console.print(f"\n[bold]Rates model (list$):[/bold] {rates.short_label()}")
    console.print(
        f"[bold]Effective rates (est$):[/bold] "
        f"input ${eff.uncached_input:.2f} / cached ${eff.cached_input:.2f} / "
        f"out ${eff.output:.2f} per 1M  "
        f"[dim](= list × {cash_scale_val:.4g}; uniform scale)[/dim]"
    )
    console.print(
        f"[bold]Cash scale (est$):[/bold] {cash_scale_val:.4g}  "
        f"[dim]({cash_scale_src})[/dim]"
    )
    console.print(
        f"[bold]TOTALS[/bold]  prompts={tot.n:,}  tokens={tot.total:,}  "
        f"cache={tot.cache_pct:.1f}%  "
        f"list$=${list_total:.2f}  "
        f"[bold green]est$=${est_total:.2f}[/bold green]"
    )
    if show_invoice:
        console.print(
            f"  Invoice allocation: ${_invoice_total(invoice_usd, fixed_usd):.2f} "
            f"(var by ticks + fixed amortized) — relative only"
        )
    if list_price:
        console.print(
            f"  Ticks/1e9: ${list_price_usd(tot.ticks):,.2f}  "
            f"[dim](usually overstates cash)[/dim]"
        )

    if api_estimate:
        _print_api_breakdown(tot, rates, rates_label)

    if advisor is not None:
        _print_plan_advisor(advisor)

    console.print()
    console.print(COST_CAVEATS_SHORT, style="dim", markup=False)

def _invoice_total(invoice_usd: float | None, fixed_usd: float) -> float:
    return float(invoice_usd or 0) + float(fixed_usd or 0)


def _print_plan_advisor(a: PlanAdvisorResult) -> None:
    """Human panel: pure API vs SuperGrok vs Heavy."""
    winner_labels = {
        "api_est": "Pure API (est$)",
        "supergrok": "SuperGrok $30 + tops",
        "heavy": "SuperGrok Heavy",
    }
    console.print()
    t = make_table(
        f"Plan advisor (projected {a.project_days}d · window {a.window_days}d · same mix)",
        ["Option", "Projected /mo", "Notes"],
    )
    t.add_row(
        "Pure API (list$)",
        f"${a.api_list_monthly:.2f}",
        "list rates × local tokens",
    )
    t.add_row(
        "Pure API (est$)",
        f"${a.api_est_monthly:.2f}",
        f"list$ × cash_scale {a.cash_scale:g}",
    )
    sg_note = (
        f"weekly include ~${a.supergrok.weekly_include_usd:g}"
        + (
            f" → +${a.supergrok.overage_list_usd:.0f} list tops"
            if a.supergrok.overage_list_usd > 0.5
            else " → top-offs common at high volume"
        )
    )
    t.add_row(
        f"SuperGrok ${a.supergrok.sub_usd:g} + tops",
        f"${a.supergrok.monthly:.2f}",
        sg_note,
    )
    hv_note = (
        f"weekly include ~${a.heavy.weekly_include_usd:g}"
        + (
            f"; +${a.heavy.overage_list_usd:.0f} overage"
            if a.heavy.overage_list_usd > 0.5
            else "; tops rare (safety net)"
        )
    )
    t.add_row(
        f"SuperGrok Heavy ${a.heavy.sub_usd:g}",
        f"${a.heavy.monthly:.2f}",
        hv_note,
    )
    console.print(t)

    console.print(
        f"  Window: list$ ${a.list_usd:.2f}  est$ ${a.est_usd:.2f}  "
        f"tokens {_fmt_tokens(a.tokens)}  cache {a.cache_pct:.1f}%"
    )
    console.print(
        f"  Run-rate: ~${a.daily_list:.2f} list/day · ~${a.daily_est:.2f} est/day · "
        f"~{_fmt_tokens(int(a.daily_tokens))} tok/day"
    )
    wlabel = winner_labels.get(a.winner, a.winner)
    # Soft language: projection only holds if this window's intensity continues
    if a.save_vs_api_est > 0.5:
        console.print(
            f"  [bold]Best fit for this window[/bold] (if intensity holds): {wlabel}  "
            f"(~${a.save_vs_api_est:.0f}/mo under est$ API at this run-rate)"
        )
    elif a.save_vs_api_est < -0.5 and a.winner == "api_est":
        console.print(
            f"  [bold]Best fit for this window[/bold] (if intensity holds): {wlabel}  "
            f"(pay-as-you-go; no flat $300 commitment)"
        )
    else:
        console.print(
            f"  [bold]Best fit for this window[/bold] (if intensity holds): {wlabel}  "
            f"(roughly break-even with est$ API at this run-rate)"
        )
    if a.heavy_cheaper_than_list_api:
        console.print(
            f"  Heavy undercuts [bold]list[/bold] API only while monthly list$ stays ≳ "
            f"${a.heavy.sub_usd:g} (this window projects ${a.api_list_monthly:.0f}/mo)"
        )
    if a.heavy_breakeven_tokens_monthly is not None:
        console.print(
            f"  ≈ Heavy vs list API break-even ~{_fmt_tokens(int(a.heavy_breakeven_tokens_monthly))} "
            f"tokens/mo at this cache mix (below that, pure API often wins)"
        )
    console.print(
        "  [dim]Caveat: assumes this window's run-rate continues. "
        "If usage is lower or highly variable month to month, pure API (est$) "
        "is usually safer — no flat subscription. "
        "Weekly pool $ are estimates; top-offs at list rates after 100%. "
        "Confirm plan prices on x.ai.[/dim]"
    )


def _print_api_breakdown(tot: UsageBucket, rates: TokenRates, rates_label: str) -> None:
    c_cached = tot.cached / 1e6 * rates.cached_input
    c_uncached = tot.uncached_in / 1e6 * rates.uncached_input
    c_out = (tot.output + tot.reasoning) / 1e6 * rates.output
    c_tot = c_cached + c_uncached + c_out
    console.print(f"\n[bold]PURE API ESTIMATE[/bold] — rates model: [cyan]{rates_label}[/cyan]")
    console.print(f"  {rates.short_label()}")
    console.print(
        f"  Cached input  {tot.cached:>14,} × ${rates.cached_input:.2f}/1M = ${c_cached:,.2f}"
    )
    console.print(
        f"  Uncached in   {tot.uncached_in:>14,} × ${rates.uncached_input:.2f}/1M = ${c_uncached:,.2f}"
    )
    console.print(
        f"  Out+reasoning {tot.output + tot.reasoning:>14,} × ${rates.output:.2f}/1M = ${c_out:,.2f}"
        f"  (out {tot.output:,} + reason {tot.reasoning:,})"
    )
    console.print(f"  Modeled API total                          ${c_tot:,.2f}")
    console.print(
        f"  Session log primary model id (info only): {tot.primary_model()}  "
        f"— api$ uses --rates-model, not mixed per-turn models"
    )


def _cost_rough(
    ctx: typer.Context,
    *,
    since: str | None,
    by: str,
    top: int,
    json_out: bool,
) -> None:
    """Legacy message×400 × static MODEL_PRICES estimate."""
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)
    sessions = list(iter_sessions(grok_home))

    if since:
        try:
            cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            sessions = [
                s
                for s in sessions
                if (s.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
            ]
        except (ValueError, TypeError, OverflowError):
            warn(f"Ignoring bad --since {since}")

    if not sessions:
        warn("No data.")
        return

    from collections import defaultdict as _dd

    groups: _dd[str, list[SessionSummary]] = _dd(list)
    for s in sessions:
        key = s.current_model_id if by == "model" else s.cwd
        groups[key].append(s)

    rows = []
    total_est = 0.0
    for k, ss in groups.items():
        tokens = sum(s.num_messages for s in ss) * 400
        model = ss[0].current_model_id if ss else "grok-build"
        est = estimate_cost(tokens, model=model, is_output=True)
        est += estimate_cost(int(tokens * 0.6), model=model, is_output=False)
        total_est += est
        rows.append((k, len(ss), round(est, 2)))

    rows.sort(key=lambda r: -r[2])

    if json_out:
        import json

        print(
            json.dumps(
                {
                    "mode": "rough",
                    "estimated_total_usd": round(total_est, 2),
                    "by": by,
                    "top": rows[:top],
                },
                indent=2,
            )
        )
        return

    t = make_table(f"Estimated Cost by {by} (ROUGH proxy, USD)", ["Key", "Sessions", "Est. $"])
    for k, ns, est in rows[:top]:
        t.add_row(k[:48] + ("…" if len(k) > 48 else ""), str(ns), f"{est:.2f}")
    console.print(t)
    console.print(f"\n[bold]Grand total (rough proxy): ${total_est:.2f}[/bold]")
    warn(
        "Legacy rough mode (message count × assumed tokens × static prices). "
        "Prefer default token mode. See: grok-utils usage info"
    )
