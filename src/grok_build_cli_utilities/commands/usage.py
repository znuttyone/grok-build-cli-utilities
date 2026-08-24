"""grok-utils usage - gorgeous analytics for Grok Build power users."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.progress import Progress
from rich.table import Table

from ..utils.auth_status import iso_or_none
from ..utils.common import (
    console,
    format_age,
    get_grok_home,
    get_sessions_dir,
    iter_sessions,
    make_table,
    warn,
)
from ..utils.pricing import (
    DEFAULT_CASH_SCALE,
    effective_rates,
    list_rate_profiles,
    load_plan_advisor_config,
    plan_advisor,
    resolve_topoff_discount_scenarios,
    resolve_topoff_pack_usd,
)
from ..utils.usage_cost_window import api_scale_for_advisor, build_token_cost_window
from ..utils.usage_display import (
    cfg_overage_scale,
    fmt_tokens,
    format_auth_plan_advisor_line,
    plan_advisor_export,
    print_api_breakdown,
    print_plan_advisor,
    print_token_cost_summary,
    week_list_series,
)
from ..utils.usage_faq import COST_CAVEATS_LONG
from ..utils.usage_tokens import (
    UsageRec,
    allocate_invoice,
    filter_usage,
    list_price_usd,
    load_turn_usage,
    parse_iso_date,
)
from .usage_legacy import print_cost_rough, print_legacy_session_report

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
    return fmt_tokens(n)


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
    by: str = typer.Option(
        "app",
        "--by",
        help=(
            "Group by: app (short name, default, list$/est$) | project (full cwd, "
            "list$/est$ with --tokens) | model | day. "
            "--tokens required for project/model/day; ignored with --by app"
        ),
    ),
    top: int = typer.Option(10, "--top", help="Show top N"),
    tokens: bool = typer.Option(
        False,
        "--tokens",
        help=(
            "Use turn-level tokens + list$/est$ (same as usage cost). "
            "Redundant with --by app (already on). Needed for --by project|model|day "
            "to leave the legacy session-summary path"
        ),
    ),
    rates_model: str | None = typer.Option(
        None,
        "--rates-model",
        "-m",
        help=(
            "Force a reconstructed list-rate table (ignores costUsdTicks). "
            f"Omit to use Build /usage Cost (ticks÷1e10). "
            f"Choices: {', '.join(list_rate_profiles())}"
        ),
    ),
    cash_scale: float | None = typer.Option(
        None,
        "--cash-scale",
        metavar="SCALE",
        help=(
            "Requires number: force uniform list$ → est$ scale (else path/regime "
            "auth mix like usage cost). Optional: usage.cash_scale in toml"
        ),
    ),
    json_out: bool = typer.Option(False, "--json", help="Flag (no value): machine-readable JSON"),
) -> None:
    """Generate a rich usage report.

    Token path (list$ + est$, same path/regime + auth mix as usage cost):
      · always when --by app
      · also when --tokens (for --by project | model | day)
    Without --tokens, --by project|model|day uses the legacy session-summary path
    (message counts, not list$/est$). Share bars use list$ on the token path.
    """
    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)

    # --by app implies token path; --tokens is only meaningful for other --by values
    if tokens and by == "app":
        warn(
            "--tokens is ignored with --by app (token path / list$/est$ is already on). "
            "Use --tokens when grouping by project, model, or day, e.g.\n"
            "  grok-utils usage report --by day --tokens --from 2026-08-01"
        )
    use_tokens = bool(tokens) or by == "app"

    if use_tokens:
        group = by if by in ("app", "project", "model", "day") else "app"
        records, d_from, d_to, data_earliest, data_latest = _load_filtered_usage(
            grok_home, since=since, date_from=date_from, date_to=date_to, apps=None
        )
        if not records:
            warn("No turn usage data for report (try without --tokens for summary-based report).")
            return
        result_earliest, result_latest = _data_date_span(records)
        win = build_token_cost_window(
            grok_home,
            records,
            group=group,
            rates_model=rates_model,
            cash_scale=cash_scale,
            d_from=d_from,
            d_to=d_to,
            data_earliest=data_earliest,
            data_latest=data_latest,
            result_earliest=result_earliest,
            result_latest=result_latest,
        )

        if json_out:
            import json

            top_rows = []
            for b in win.buckets[:top]:
                list_b = win.list_for_key(b.key)
                row = b.to_dict(win.rates)
                row["list_usd"] = round(list_b, 4)
                row["api_est_usd"] = round(list_b, 4)
                row["est_usd"] = round(win.est_for_key(b.key), 4)
                top_rows.append(row)

            print(
                json.dumps(
                    {
                        "mode": "tokens",
                        "by": group,
                        "from": d_from.isoformat() if d_from else None,
                        "to": d_to.isoformat() if d_to else None,
                        "result_from": (result_earliest.isoformat() if result_earliest else None),
                        "result_to": (result_latest.isoformat() if result_latest else None),
                        "rates_model": win.rates_label,
                        "list_source": win.list_source,
                        "rates": win.rates.as_dict(),
                        "cash_scale": win.cash_scale_val,
                        "cash_scale_source": win.cash_scale_src,
                        "topoff_discount": win.topoff_d,
                        "topoff_discount_source": win.topoff_src,
                        "auth": win.auth_st.as_dict(),
                        "weekly_usage_pct": win.weekly_pct,
                        "weekly_period_start": iso_or_none(win.weekly_period_start),
                        "weekly_resets_at": iso_or_none(win.weekly_period_end),
                        "prepaid_balance_usd": win.prepaid_balance,
                        "auth_mix": win.mix.as_dict(),
                        "totals": {
                            **win.tot.to_dict(win.rates),
                            "list_usd": round(win.list_total, 4),
                            "api_est_usd": round(win.list_total, 4),
                            "est_usd": round(win.est_total, 4),
                            "est_cash_usd": round(win.est_cash_total, 4),
                        },
                        "buckets": top_rows,
                        "caveats": [
                            "list$_is_costUsdTicks_div_1e10_when_present_else_rates",
                            "est$_uses_auth_timeline_mix_unless_uniform_override",
                            "share_bars_use_list$",
                            "est_cash$_applies_topoff_discount_to_est$",
                        ],
                    },
                    indent=2,
                )
            )
            return

        period = ""
        if result_earliest or result_latest:
            left = result_earliest.isoformat() if result_earliest else "…"
            right = result_latest.isoformat() if result_latest else "…"
            period = f" · {left} → {right}"
            if d_from is not None and result_earliest is not None and d_from < result_earliest:
                period += f"  (requested --from {d_from.isoformat()})"
        title = (
            f"Usage by {group} (list$ primary · est$=path scale, top {top}, "
            f"{win.tot.n} prompts, {_fmt_tokens(win.tot.total)} tok){period}"
        )
        t = make_table(
            title,
            ["Key", "Prm", "Tokens", "Cache%", "list$", "est$", "Share(list$)", "Share(tok)"],
        )
        max_list = max((win.list_for_key(b.key) for b in win.buckets[:top]), default=1.0) or 1.0
        max_tok = max((b.total for b in win.buckets[:top]), default=1) or 1
        for b in win.buckets[:top]:
            list_b = win.list_for_key(b.key)
            est_b = win.est_for_key(b.key)
            key = b.key[:44] + ("…" if len(b.key) > 44 else "")
            t.add_row(
                key,
                str(b.n),
                _fmt_tokens(b.total),
                f"{b.cache_pct:.1f}%",
                f"{list_b:.2f}",
                f"{est_b:.2f}",
                _ascii_bar(list_b, max_list, 12),
                _ascii_bar(float(b.total), float(max_tok), 12),
            )
        console.print(t)
        print_token_cost_summary(win, cost_mode=False, show_faq_hint=False)
        from ..utils.usage_tokens import aggregate as _agg

        day_buckets = _agg(records, "day")
        vals = [b.total for b in day_buckets[-14:]]
        if vals:
            console.print(
                f"[dim]Daily tokens[/dim]  {_sparkline(vals)}  "
                f"(last {len(vals)}d · max {_fmt_tokens(max(vals))})"
            )
        console.print(
            "\n[dim]FAQ: grok-utils usage info"
            "  ·  cost detail: grok-utils usage cost … -P"
            "  ·  wallet: grok-utils auth status[/dim]"
        )
        return

    # Legacy summary.json path (sessions/messages only)
    print_legacy_session_report(
        grok_home,
        since=since,
        date_from=date_from,
        by=by,
        top=top,
        json_out=json_out,
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
    rates_model: str | None = typer.Option(
        None,
        "--rates-model",
        "-m",
        metavar="MODEL",
        help=(
            "Force a reconstructed list-rate table (ignores costUsdTicks). "
            f"Omit to use Build /usage Cost (ticks÷1e10). "
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
    topoff_discount: float | None = typer.Option(
        None,
        "--topoff-discount",
        metavar="FRAC",
        help=(
            "Requires 0..1: model Extra Credits pack promo "
            "(0=full price, 0.25=−25%, 1.0=free tops). "
            "Default 0; or usage.topoff_discount in toml. Affects est_cash$ + plan scenarios."
        ),
    ),
    list_price: bool = typer.Option(
        False,
        "--list-price",
        help="Flag (no value): show costUsdTicks÷1e10 (same $ as /usage Session Cost)",
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
    detail: bool = typer.Option(
        False,
        "--detail",
        "-v",
        help=(
            "Flag (no value): verbose footer — full auth-mix lines, promo/overage "
            "scenarios, Heavy break-even, long caveats"
        ),
    ),
    app: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--app",
        metavar="NAME",
        help="Requires substring: filter by app/project (repeatable, e.g. --app VCI)",
    ),
    json_out: bool = typer.Option(False, "--json", help="Flag (no value): machine-readable JSON"),
) -> None:
    """Token-accurate cost: list$ (API list rates) + est$ (path/regime spend lens).

    Options that take a value need the number/date on the same flag
    (e.g. --invoice-usd 180, not bare --invoice-usd). Use: grok-utils usage cost --help

    list$ = Build costUsdTicks ÷ 1e10 (same $ as /usage Session Cost).
            Pass -m MODEL to reconstruct from a published rate table instead.
    est$  = list$ × path/regime scale (API≈1.0; SuperGrok pool≈0; overage≈1.9)
            via auth timeline mix unless --cash-scale / prepaid-fit forces one scale.
    Footer: Wallet / auth snapshot (Extra Credits · weekly % · path) + weekly pool resets line. FAQ: usage info

      # Closed window
      grok-utils usage cost --from 2026-08-01 --to 2026-08-05 --by app

      # From a date through latest session data (omit --to)
      grok-utils usage cost --from 2026-08-01 --by app

      # Plan advisor: API vs SuperGrok vs Heavy for the window
      grok-utils usage cost --from 2026-07-18 --by app --plan-advisor

      # One-shot wallet fit for a window (both amounts required)
      grok-utils usage cost ... --prepaid-usd 70 --credits-remaining 21.40

      # Model promo card cost (−25% or free tops) for est_cash$ / plan-advisor
      grok-utils usage cost ... -P --topoff-discount 0.25
      grok-utils usage cost ... -P --topoff-discount 1.0

      # Invoice allocation (amount required)
      grok-utils usage cost ... --invoice-usd 180 --fixed-usd 30

    See: grok-utils usage info
    """

    grok_home = get_grok_home(ctx.obj.get("grok_home") if ctx.obj else None)

    if mode == "rough":
        print_cost_rough(
            ctx,
            since=since or date_from,
            by=by if by in ("model", "project") else "model",
            top=top,
            json_out=json_out,
        )
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

    result_earliest, result_latest = _data_date_span(records)
    win = build_token_cost_window(
        grok_home,
        records,
        group=by,
        rates_model=rates_model,
        cash_scale=cash_scale,
        prepaid_usd=prepaid_usd,
        credits_remaining=credits_remaining,
        topoff_discount=topoff_discount,
        d_from=d_from,
        d_to=d_to,
        data_earliest=data_earliest,
        data_latest=data_latest,
        result_earliest=result_earliest,
        result_latest=result_latest,
    )
    rates = win.rates
    rates_label = win.rates_label
    list_total = win.list_total
    est_total = win.est_total
    est_cash_total = win.est_cash_total
    tot = win.tot
    buckets = win.buckets
    mix = win.mix
    cash_scale_val = win.cash_scale_val
    cash_scale_src = win.cash_scale_src
    force_uniform = win.force_uniform
    topoff_d = win.topoff_d
    topoff_src = win.topoff_src
    auth_st = win.auth_st
    weekly_pct = win.weekly_pct
    prepaid_balance = win.prepaid_balance
    usage_cfg = win.usage_cfg
    topoff_scenarios = resolve_topoff_discount_scenarios(
        usage_cfg, active_discount=topoff_d if topoff_d > 0 else None
    )

    scale_api, advisor_est = api_scale_for_advisor(win)
    advisor = None
    if plan_advisor_flag:
        if result_earliest is None or result_latest is None:
            warn("Plan advisor needs dated turns; skipping.")
        else:
            window_days = (result_latest - result_earliest).days + 1
            pa_cfg = load_plan_advisor_config(usage_cfg)
            advisor = plan_advisor(
                list_usd=list_total,
                est_usd=advisor_est,
                tokens=tot.total,
                cache_pct=tot.cache_pct,
                cash_scale=scale_api,
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

    if json_out:
        import json

        top_rows = []
        for b in buckets[:top]:
            list_b = win.list_for_key(b.key)
            row = b.to_dict(rates)
            row["list_usd"] = round(list_b, 4)
            row["est_usd"] = round(win.est_for_key(b.key), 4)
            # Per-app regime list$ shares (reconciled %)
            path_map = mix.list_by_key_path.get(b.key) or {}
            if path_map:
                names = list(path_map.keys())
                weights = [path_map[n] for n in names]
                from ..utils.usage_display import _reconcile_pcts

                pcts = _reconcile_pcts(weights)
                row["regime_list_pct"] = {n: p for n, p in zip(names, pcts, strict=True) if p > 0}
                row["regime_list_usd"] = {n: round(path_map[n], 4) for n in names}
            if show_invoice:
                var = b.ticks * inv_scale
                row["variable_usd"] = round(var, 4)
                row["total_usd"] = round(var + fixed_per, 4)
            if list_price:
                row["list_price_usd"] = round(list_price_usd(b.ticks), 4)
            top_rows.append(row)

        weeks = week_list_series(records, rates, prefer_ticks=win.prefer_ticks)
        pack_usd = resolve_topoff_pack_usd(usage_cfg)
        ov_scale = cfg_overage_scale(usage_cfg)
        plan_export = None
        if advisor is not None:
            plan_export = plan_advisor_export(
                advisor,
                topoff_scenarios=topoff_scenarios,
                active_topoff_discount=topoff_d,
                pack_usd=pack_usd,
                overage_scale=ov_scale,
                week_list=weeks,
                list_by_key_path=dict(mix.list_by_key_path),
                current_tier=win.subscription_tier,
                window_tiers=list(win.window_tiers),
            )

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
            "list_source": win.list_source,
            "rates": rates.as_dict(),
            "cash_scale": cash_scale_val,
            "cash_scale_source": cash_scale_src,
            "topoff_discount": topoff_d,
            "topoff_discount_source": topoff_src,
            "topoff_pack_usd": pack_usd,
            "effective_rates": (
                effective_rates(rates, cash_scale_val).as_dict()
                if force_uniform is not None and cash_scale_val > 0
                else None
            ),
            "auth": auth_st.as_dict(),
            "wallet": {
                "extra_credits_remaining_usd": prepaid_balance,
                "weekly_supergrok_limit_pct_used": weekly_pct,
                "weekly_limit_pct_used": weekly_pct,
                "weekly_period_start": iso_or_none(win.weekly_period_start),
                "weekly_resets_at": iso_or_none(win.weekly_period_end),
                "subscription_tier": win.subscription_tier,
                "subscription_tier_raw": win.subscription_tier_raw,
                "subscription_tier_label": (
                    "Heavy"
                    if win.subscription_tier == "heavy"
                    else ("SuperGrok" if win.subscription_tier == "supergrok" else None)
                ),
                "auth_path": auth_st.effective,
            },
            "weekly_usage_pct": weekly_pct,
            "weekly_resets_at": iso_or_none(win.weekly_period_end),
            "subscription_tier": win.subscription_tier,
            "prepaid_balance_usd": prepaid_balance,
            "topoff_discount_scenarios": topoff_scenarios,
            "auth_mix": mix.as_dict(),
            "week_list_usd": [{"week": w, "list_usd": round(u, 4)} for w, u in weeks],
            "totals": {
                **tot.to_dict(rates),
                "list_usd": round(list_total, 4),
                "api_est_usd": round(list_total, 4),
                "est_usd": round(est_total, 4),
                "est_cash_usd": round(est_cash_total, 4),
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
            "plan_advisor": plan_export
            if plan_export
            else (advisor.as_dict() if advisor else None),
            "caveats": [
                "list$_is_costUsdTicks_div_1e10_when_present_else_rates",
                "est$_uses_auth_timeline_mix_unless_uniform_override",
                "weekly_pct_unknown_uses_list_scale_not_pool_or_overage",
                "subscription_tier_from_billing_log_not_session_turns",
                "plan_advisor_pure_api_uses_api_scale_not_table_scale",
                "topoff_discount_is_card_promo_not_list$",
                "pass_-m_to_force_reconstructed_rate_table",
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    period = ""
    if result_earliest or result_latest or d_from or d_to:
        left = (
            result_earliest.isoformat()
            if result_earliest
            else (d_from.isoformat() if d_from else "…")
        )
        right = result_latest.isoformat() if result_latest else (d_to.isoformat() if d_to else "…")
        period = f" · {left} → {right}"
        if d_from is not None and result_earliest is not None and d_from < result_earliest:
            period += f"  (requested --from {d_from.isoformat()})"
    headers = ["Key", "Prm", "Tokens", "Cache%", "list$", "est$"]
    if show_invoice:
        headers += ["var$", "tot$"]
    if list_price:
        headers.append("ticks$")
    headers.append("Share(list$)")

    t = make_table(
        f"Estimated Cost by {by} (list$ primary · est$=path scale){period}",
        headers,
    )
    share_vals = [win.list_for_key(b.key) for b in buckets[:top]]
    max_share = max(share_vals, default=1.0) or 1.0
    for b in buckets[:top]:
        list_b = win.list_for_key(b.key)
        est_b = win.est_for_key(b.key)
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
        cells.append(_ascii_bar(list_b, max_share, 12))
        t.add_row(*cells)
    console.print(t)

    print_token_cost_summary(win, detail=detail, cost_mode=True, show_faq_hint=False)
    if show_invoice:
        console.print(
            f"  Invoice allocation: ${_invoice_total(invoice_usd, fixed_usd):.2f} "
            f"(var by ticks + fixed amortized) — relative only"
        )
    if list_price:
        console.print(
            f"  costUsdTicks÷1e10: ${list_price_usd(tot.ticks):,.4f}  "
            f"[dim](same unit as /usage Session Cost)[/dim]"
        )

    if api_estimate:
        print_api_breakdown(tot, rates, rates_label)

    if advisor is not None:
        print_plan_advisor(
            advisor,
            auth_line=format_auth_plan_advisor_line(
                auth_st, subscription_tier=win.subscription_tier
            ),
            overage_scale=cfg_overage_scale(usage_cfg),
            topoff_scenarios=topoff_scenarios,
            active_topoff_discount=topoff_d,
            detail=detail,
            mix_slices=list(mix.slices),
            pack_usd=resolve_topoff_pack_usd(usage_cfg),
            week_list=week_list_series(records, rates),
            list_by_key_path=dict(mix.list_by_key_path),
            current_tier=win.subscription_tier,
            window_tiers=list(win.window_tiers),
        )

    if detail:
        console.print(
            "\n[dim]FAQ / ledgers: grok-utils usage info"
            "  ·  wallet + history: grok-utils auth status [--history]"
            "  ·  tune: --cash-scale / --topoff-discount / toml[/dim]"
        )
    else:
        console.print(
            "\n[dim]FAQ: grok-utils usage info"
            "  ·  more: --detail / -v"
            "  ·  wallet: grok-utils auth status[/dim]"
        )


def _invoice_total(invoice_usd: float | None, fixed_usd: float) -> float:
    return float(invoice_usd or 0) + float(fixed_usd or 0)
