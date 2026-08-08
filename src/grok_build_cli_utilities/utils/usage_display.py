"""Human rendering for usage cost / token report footers and plan-advisor."""

from __future__ import annotations

from typing import Any

from .auth_status import format_auth_plan_advisor_line, format_auth_short
from .common import console, make_table
from .pricing import (
    DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE,
    PlanAdvisorResult,
    TokenRates,
    cfg_float,
    topoff_discount_label,
)
from .usage_cost_window import TokenCostWindow
from .usage_tokens import UsageBucket


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def print_auth_mix_summary(
    mix: Any,
    *,
    list_total: float,
    force_uniform: bool,
    detail: bool = False,
) -> None:
    """Compact auth-mix line; richer one-liner pieces with detail=True."""
    if force_uniform or not mix.slices:
        return
    labels = {
        "api_key": "API",
        "supergrok_session": "SuperGrok",
        "supergrok_pool": "SuperGrok pool",
        "supergrok_overage": "SuperGrok overage",
        "supergrok_unknown": "SuperGrok (?)",
        "unknown": "unknown",
        "uniform": "uniform",
    }
    parts: list[str] = []
    for s in mix.slices:
        lab = labels.get(s.path, s.path)
        pct = (100.0 * s.list_usd / list_total) if list_total > 0 else 0.0
        if detail:
            parts.append(
                f"{lab} prm={s.prompts} tok={fmt_tokens(s.tokens)} "
                f"list${s.list_usd:.2f}→est${s.est_usd:.2f} "
                f"({pct:.0f}% list · ×{s.scale:.2g})"
            )
        else:
            parts.append(f"{lab} est${s.est_usd:.2f} ({pct:.0f}% list · ×{s.scale:.2g})")
    if parts:
        sep = " · " if detail else " + "
        console.print(f"[bold]est$ mix[/bold]  {sep.join(parts)}")


def print_wallet_auth_line(win: TokenCostWindow) -> None:
    """Wallet / auth  Extra Credits $… · weekly N% · SuperGrok session"""
    snap_parts: list[str] = []
    if win.prepaid_balance is not None:
        snap_parts.append(f"Extra Credits ${win.prepaid_balance:.2f}")
    if win.weekly_pct is not None:
        snap_parts.append(f"weekly {win.weekly_pct:g}%")
    auth_lab = {
        "supergrok_session": "SuperGrok session",
        "api_key": "API key",
        "none": "no auth",
    }.get(win.auth_st.effective, win.auth_st.effective)
    snap_parts.append(auth_lab)
    console.print(f"[bold]Wallet / auth[/bold]  {' · '.join(snap_parts)}")
    if win.mix.source == "auth_mix" and win.est_total + 0.01 < win.list_total * 0.5:
        console.print(
            "[dim]list$ = activity · est$ ≈ Extra Credits burn "
            "(0 while SuperGrok weekly pool has room)[/dim]"
        )
    if any(
        s.path in ("supergrok_unknown", "unknown")
        or "unknown" in (s.scale_src or "")
        for s in win.mix.slices
    ):
        console.print(
            "[dim]Some turns lack weekly%/auth history — est$ uses list$ there[/dim]"
        )


def print_token_cost_summary(
    win: TokenCostWindow,
    *,
    detail: bool = False,
    show_faq_hint: bool = True,
    cost_mode: bool = False,
) -> None:
    """Shared TOTALS / rates / mix / wallet footer after a token table."""
    tot = win.tot
    tot_line = (
        f"\n[bold]TOTALS[/bold]  prompts={tot.n:,}  tokens={fmt_tokens(tot.total)}  "
        f"cache={tot.cache_pct:.1f}%  "
        f"list$=${win.list_total:.2f}  "
        f"[bold green]est$=${win.est_total:.2f}[/bold green]"
    )
    if abs(win.est_cash_total - win.est_total) > 0.005 or win.topoff_d > 0:
        tot_line += f"  est_cash$=${win.est_cash_total:.2f}"
    console.print(tot_line)
    console.print(
        f"[dim]Rates[/dim]  {win.rates.short_label()}"
        + (
            f"  ·  forced scale {win.cash_scale_val:.4g} ({win.cash_scale_src})"
            if win.force_uniform is not None
            else "  ·  est$ = path/regime mix (Share = list$)"
        )
    )
    print_auth_mix_summary(
        win.mix,
        list_total=win.list_total,
        force_uniform=win.force_uniform is not None,
        detail=detail,
    )
    print_wallet_auth_line(win)
    if win.topoff_d > 0:
        console.print(
            f"[dim]Top-off promo[/dim]  {win.topoff_d:g} ({win.topoff_src}) → est_cash$"
        )
    if show_faq_hint:
        if detail:
            console.print(
                "\n[dim]FAQ / ledgers: grok-utils usage info"
                "  ·  wallet + history: grok-utils auth status [--history]"
                "  ·  tune: --cash-scale / --topoff-discount / toml[/dim]"
            )
        elif cost_mode:
            console.print(
                "\n[dim]FAQ: grok-utils usage info"
                "  ·  more: --detail / -v"
                "  ·  wallet: grok-utils auth status[/dim]"
            )
        else:
            console.print(
                "\n[dim]FAQ: grok-utils usage info"
                "  ·  cost detail: grok-utils usage cost … -P"
                "  ·  wallet: grok-utils auth status[/dim]"
            )


def print_auth_block_status(status: Any) -> None:
    console.print()
    for line in format_auth_short(status):
        console.print(line, style="dim", markup=False)


def cfg_overage_scale(usage_cfg: dict) -> float:
    return cfg_float(
        usage_cfg or {},
        "cash_scale_supergrok_overage",
        DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE,
    )


def _plan_row(
    t: Any,
    *,
    option: str,
    monthly: float,
    notes: str,
    highlight: bool,
) -> None:
    """Add a plan-advisor row; ★ + bold green on the winning line."""
    if highlight:
        t.add_row(
            f"[bold green]★ {option}[/bold green]",
            f"[bold green]${monthly:.0f}[/bold green]",
            f"[green]{notes}  ← best fit[/green]",
        )
    else:
        t.add_row(option, f"${monthly:.0f}", notes)


def print_plan_advisor(
    a: PlanAdvisorResult,
    *,
    auth_line: str | None = None,
    overage_scale: float = 1.9,
    topoff_scenarios: list[float] | None = None,
    active_topoff_discount: float = 0.0,
    detail: bool = False,
) -> None:
    """Plan comparison: compact by default; promo/overage with --detail."""
    winner_labels = {
        "api_est": "Pure API",
        "supergrok": f"SuperGrok ${a.supergrok.sub_usd:g}",
        "heavy": f"Heavy ${a.heavy.sub_usd:g}",
    }
    active_d = float(active_topoff_discount or 0.0)
    api_mo = a.api_est_monthly
    sg_promo = a.supergrok.sub_usd + a.supergrok.overage_list_usd * (1.0 - active_d)
    hv_promo = a.heavy.sub_usd + a.heavy.overage_list_usd * (1.0 - active_d)
    pct = int(round(active_d * 100)) if active_d > 0 else 0

    # Which table row is primary best-fit?
    if active_d > 0:
        win_key = min(
            ("api", api_mo),
            ("sg_promo", sg_promo),
            ("hv_promo", hv_promo),
            key=lambda kv: kv[1],
        )[0]
    else:
        win_key = {
            "api_est": "api",
            "supergrok": "sg_full",
            "heavy": "hv_full",
        }.get(a.winner, "api")

    console.print()
    t = make_table(
        f"Plan advisor · if this {a.window_days}d pace holds → {a.project_days}d",
        ["Option", "$/mo", "Notes"],
    )
    api_same = abs(a.api_list_monthly - a.api_est_monthly) < 0.05
    if api_same:
        _plan_row(
            t,
            option="Pure API",
            monthly=a.api_list_monthly,
            notes="list rates · paygo",
            highlight=win_key == "api",
        )
    else:
        t.add_row(
            "Pure API (list$)",
            f"${a.api_list_monthly:.0f}",
            "list rates × tokens",
        )
        _plan_row(
            t,
            option="Pure API (est$)",
            monthly=a.api_est_monthly,
            notes=f"list$ × {a.cash_scale:g}",
            highlight=win_key == "api",
        )
    sg_note = (
        f"include ~${a.supergrok.weekly_include_usd:g}/wk"
        + (
            f" +${a.supergrok.overage_list_usd:.0f} tops"
            if a.supergrok.overage_list_usd > 0.5
            else ""
        )
    )
    hv_note = (
        f"include ~${a.heavy.weekly_include_usd:g}/wk"
        + (
            f" +${a.heavy.overage_list_usd:.0f} tops"
            if a.heavy.overage_list_usd > 0.5
            else " · tops rare"
        )
    )
    _plan_row(
        t,
        option=f"SuperGrok ${a.supergrok.sub_usd:g}",
        monthly=a.supergrok.monthly,
        notes=sg_note + " · full-price tops",
        highlight=win_key == "sg_full",
    )
    _plan_row(
        t,
        option=f"Heavy ${a.heavy.sub_usd:g}",
        monthly=a.heavy.monthly,
        notes=hv_note,
        highlight=win_key == "hv_full",
    )

    if active_d > 0:
        _plan_row(
            t,
            option=f"SuperGrok @ −{pct}% tops",
            monthly=sg_promo,
            notes=f"card = sub + tops×{1.0 - active_d:g}  ← your --topoff-discount",
            highlight=win_key == "sg_promo",
        )
        if a.heavy.overage_list_usd > 0.5:
            _plan_row(
                t,
                option=f"Heavy @ −{pct}% tops",
                monthly=hv_promo,
                notes="card if Heavy also buys promo tops",
                highlight=win_key == "hv_promo",
            )
    console.print(t)

    wlabel = winner_labels.get(a.winner, a.winner)

    if active_d > 0:
        promo_cands = {
            "api": ("Pure API", api_mo),
            "sg_promo": (f"SuperGrok (−{pct}% tops)", sg_promo),
            "hv_promo": (f"Heavy (−{pct}% tops)", hv_promo),
        }
        win_name, win_mo = promo_cands[win_key]
        promo_save = api_mo - win_mo
        console.print(
            f"[bold green]★ Best fit[/bold green] "
            f"(if pace holds · −{pct}% tops as modeled): "
            f"[bold]{win_name}[/bold] ~${win_mo:.0f}/mo"
            + (
                f"  (~${promo_save:.0f}/mo under Pure API)"
                if promo_save > 0.5
                else ""
            )
        )
        console.print(
            f"  [dim]SuperGrok card ${sg_promo:.0f} = sub ${a.supergrok.sub_usd:g} "
            f"+ ${a.supergrok.overage_list_usd:.0f} tops × {1.0 - active_d:g}  "
            f"(e.g. $100 pack → ${100 * (1.0 - active_d):.0f} card)[/dim]"
        )
        if a.save_vs_api_est > 0.5:
            console.print(
                f"  [dim]Without promo (full-price tops): {wlabel} "
                f"~${a.save_vs_api_est:.0f}/mo under Pure API[/dim]"
            )
        else:
            console.print(
                f"  [dim]Without promo (full-price tops): {wlabel}[/dim]"
            )
    elif a.save_vs_api_est > 0.5:
        console.print(
            f"[bold green]★ Best fit[/bold green] "
            f"(if pace holds, full-price tops): {wlabel}  "
            f"~${a.save_vs_api_est:.0f}/mo under Pure API"
        )
    elif a.save_vs_api_est < -0.5 and a.winner == "api_est":
        console.print(
            f"[bold green]★ Best fit[/bold green] (if pace holds): {wlabel}  "
            f"(paygo · no flat sub)"
        )
    else:
        console.print(
            f"[bold green]★ Best fit[/bold green] "
            f"(if pace holds, full-price tops): {wlabel}  "
            f"(≈ break-even with Pure API)"
        )

    scenarios = topoff_scenarios if topoff_scenarios is not None else [0.0, 0.25, 1.0]

    if not detail:
        promo_bits: list[str] = []
        for disc in scenarios:
            if disc <= 0:
                continue
            sg_card = a.supergrok.sub_usd + a.supergrok.overage_list_usd * (1.0 - disc)
            is_active = abs(disc - active_d) < 1e-9 and disc > 0
            mark = "*" if is_active else ""
            short = "free" if disc >= 1.0 - 1e-12 else f"−{int(round(disc * 100))}%"
            promo_bits.append(f"{short} ${sg_card:.0f}{mark}")
        if promo_bits and active_d <= 0:
            # Only show multi-scenario line when no active discount (else already above)
            console.print(
                "[dim]SuperGrok card if promo tops[/dim]  "
                + " · ".join(promo_bits)
                + f"  [dim](Heavy ~${a.heavy.monthly:.0f}; best-fit = full price)[/dim]"
            )
        elif promo_bits and active_d > 0:
            others = [b for b in promo_bits if not b.endswith("*")]
            if others:
                console.print(
                    "[dim]Other promo scenarios[/dim]  " + " · ".join(others)
                )
        console.print(
            f"[dim]~${a.daily_list:.2f} list$/day · {fmt_tokens(int(a.daily_tokens))} tok/day"
            f" · quieter months → Pure API safer · pool $ estimated[/dim]"
        )
        _print_auth_now_hint(auth_line)
        return

    t2 = make_table(
        "Top-off promo (card = sub + tops×(1−discount); best-fit above = full price)",
        ["Scenario", "SuperGrok", "Heavy", "vs Pure API"],
    )
    api_mo = a.api_est_monthly
    for disc in scenarios:
        sg_card = a.supergrok.sub_usd + a.supergrok.overage_list_usd * (1.0 - disc)
        hv_card = a.heavy.sub_usd + a.heavy.overage_list_usd * (1.0 - disc)
        best_plan = min(sg_card, hv_card)
        if best_plan + 0.5 < api_mo:
            vs = f"plan −${api_mo - best_plan:.0f}"
        elif api_mo + 0.5 < best_plan:
            vs = f"API −${best_plan - api_mo:.0f}"
        else:
            vs = "≈ even"
        mark = " ★" if abs(disc - float(active_topoff_discount)) < 1e-9 and disc > 0 else ""
        t2.add_row(
            f"{topoff_discount_label(disc)}{mark}",
            f"${sg_card:.0f}",
            f"${hv_card:.0f}",
            vs,
        )
    console.print()
    console.print(t2)

    ov = max(0.0, float(overage_scale))
    face = a.api_list_monthly * ov
    d25 = face * 0.75
    be = ""
    if a.heavy_breakeven_tokens_monthly is not None:
        be = (
            f"  ·  Heavy break-even ~"
            f"{fmt_tokens(int(a.heavy_breakeven_tokens_monthly))} tok/mo"
        )
    console.print(
        f"[dim]Overage lens (all volume @ {ov:g}× list$): "
        f"face tops ~${face:.0f}/mo"
        f" · −25% card ~${d25:.0f}"
        f" · free ~$0"
        f" (+ SuperGrok sub ${a.supergrok.sub_usd:g})"
        f"{be}[/dim]"
    )
    console.print(
        f"[dim]~${a.daily_list:.2f} list$/day · {fmt_tokens(int(a.daily_tokens))} tok/day"
        f" · quieter months → Pure API safer · pool $ estimated[/dim]"
    )
    _print_auth_now_hint(auth_line)


def _print_auth_now_hint(auth_line: str | None) -> None:
    if not auth_line:
        return
    if "SuperGrok session" in auth_line:
        console.print(
            "[dim]Auth now: SuperGrok session — Pure API is counterfactual "
            "unless you switch[/dim]"
        )
    elif "API key" in auth_line:
        console.print(
            "[dim]Auth now: API key — SuperGrok/Heavy are counterfactual "
            "unless you grok login[/dim]"
        )


def print_api_breakdown(tot: UsageBucket, rates: TokenRates, rates_label: str) -> None:
    c_cached = tot.cached / 1e6 * rates.cached_input
    c_uncached = tot.uncached_in / 1e6 * rates.uncached_input
    c_out = (tot.output + tot.reasoning) / 1e6 * rates.output
    c_tot = c_cached + c_uncached + c_out
    console.print(f"\n[bold]PURE API ESTIMATE[/bold] — rates model: [cyan]{rates_label}[/cyan]")
    console.print(f"  {rates.short_label()}")
    console.print(
        f"  Cached input  {tot.cached:>14,} × ${rates.cached_input:.2f}/1M "
        f"= ${c_cached:,.2f}"
    )
    console.print(
        f"  Uncached in   {tot.uncached_in:>14,} × ${rates.uncached_input:.2f}/1M "
        f"= ${c_uncached:,.2f}"
    )
    out_n = tot.output + tot.reasoning
    console.print(
        f"  Out+reasoning {out_n:>14,} × ${rates.output:.2f}/1M = ${c_out:,.2f}"
        f"  (out {tot.output:,} + reason {tot.reasoning:,})"
    )
    console.print(f"  Modeled API total                          ${c_tot:,.2f}")
    console.print(
        f"  Session log primary model id (info only): {tot.primary_model()}  "
        f"— api$ uses --rates-model, not mixed per-turn models"
    )


# re-export for plan-advisor auth line
__all__ = [
    "cfg_overage_scale",
    "fmt_tokens",
    "format_auth_plan_advisor_line",
    "print_api_breakdown",
    "print_auth_block_status",
    "print_auth_mix_summary",
    "print_plan_advisor",
    "print_token_cost_summary",
    "print_wallet_auth_line",
]
