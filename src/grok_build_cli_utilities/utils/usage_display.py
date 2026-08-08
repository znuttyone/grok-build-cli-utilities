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
)
from .usage_cost_window import TokenCostWindow
from .usage_tokens import UsageBucket


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _reconcile_pcts(weights: list[float]) -> list[int]:
    """Largest-remainder integers that sum to 100 (or 0 if all zero)."""
    total = sum(weights)
    if total <= 0:
        return [0] * len(weights)
    raw = [100.0 * w / total for w in weights]
    floors = [int(x) for x in raw]
    rem = 100 - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (raw[i] - floors[i], i), reverse=True)
    out = floors[:]
    for i in order[: max(0, rem)]:
        out[i] += 1
    return out


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
    weights = [float(s.list_usd) for s in mix.slices]
    pcts = _reconcile_pcts(weights)
    parts: list[str] = []
    for s, pct in zip(mix.slices, pcts, strict=True):
        lab = labels.get(s.path, s.path)
        if detail:
            parts.append(
                f"{lab} prm={s.prompts} tok={fmt_tokens(s.tokens)} "
                f"list${s.list_usd:.2f}→est${s.est_usd:.2f} "
                f"({pct}% list · ×{s.scale:.2g})"
            )
        else:
            parts.append(f"{lab} est${s.est_usd:.2f} ({pct}% list · ×{s.scale:.2g})")
    if parts:
        sep = " · " if detail else " + "
        console.print(f"[bold]est$ mix[/bold]  {sep.join(parts)}")


def print_wallet_auth_line(win: TokenCostWindow, *, detail: bool = False) -> None:
    """Explicit wallet labels (remaining balance · weekly % used · auth path)."""
    snap_parts: list[str] = []
    if win.prepaid_balance is not None:
        snap_parts.append(f"Extra Credits remaining ${win.prepaid_balance:.2f}")
    else:
        snap_parts.append("Extra Credits remaining (no billing sample)")
    if win.weekly_pct is not None:
        snap_parts.append(f"weekly SuperGrok limit {win.weekly_pct:g}% used")
    else:
        snap_parts.append("weekly SuperGrok limit (no sample)")
    auth_lab = {
        "supergrok_session": "auth SuperGrok session",
        "api_key": "auth API key",
        "none": "auth none",
    }.get(win.auth_st.effective, f"auth {win.auth_st.effective}")
    snap_parts.append(auth_lab)
    console.print(f"[bold]Wallet / auth[/bold]  {' · '.join(snap_parts)}")
    if detail and win.mix.source == "auth_mix" and win.est_total + 0.01 < win.list_total * 0.5:
        console.print(
            "[dim]list$ = activity · est$ ≈ Extra Credits burn "
            "(0 while SuperGrok weekly pool has room)[/dim]"
        )
    has_sg_unknown = any(
        s.path == "supergrok_unknown"
        or (s.path == "unknown")
        or "unknown" in (s.scale_src or "")
        for s in win.mix.slices
    )
    if has_sg_unknown:
        console.print(
            "[dim]SuperGrok (?): weekly % unknown at turn time "
            "(before billing log / gap) → est$ uses list$ scale, "
            "not pool 0 or overage 1.9[/dim]"
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
    print_wallet_auth_line(win, detail=detail)
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


def week_list_series(
    records: list[Any],
    rates: TokenRates,
) -> list[tuple[str, float]]:
    """ISO-week list$ totals for variance context (oldest → newest)."""
    from collections import defaultdict

    from .pricing import api_estimate_usd

    by_week: dict[str, float] = defaultdict(float)
    for r in records:
        ts = getattr(r, "ts", None)
        if ts is None:
            continue
        iso = ts.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        by_week[key] += float(
            api_estimate_usd(
                cached=int(getattr(r, "cached", 0) or 0),
                uncached_in=max(
                    0,
                    int(getattr(r, "input", 0) or 0)
                    - int(getattr(r, "cached", 0) or 0),
                ),
                output=int(getattr(r, "output", 0) or 0),
                reasoning=int(getattr(r, "reasoning", 0) or 0),
                rates=rates,
            )
        )
    return sorted(by_week.items(), key=lambda kv: kv[0])


def print_plan_advisor(
    a: PlanAdvisorResult,
    *,
    auth_line: str | None = None,
    overage_scale: float = 1.9,
    topoff_scenarios: list[float] | None = None,
    active_topoff_discount: float = 0.0,
    detail: bool = False,
    mix_slices: list[Any] | None = None,
    pack_usd: float = 100.0,
    week_list: list[tuple[str, float]] | None = None,
    list_by_key_path: dict[str, dict[str, float]] | None = None,
) -> None:
    """One table: base plans + offered promos; single ★ best for window usage."""
    from .pricing import (
        DEFAULT_TOPOFF_DISCOUNT_SCENARIOS,
        DEFAULT_TOPOFF_PACK_USD,
        ceil_to_pack_usd,
    )

    active_d = float(active_topoff_discount or 0.0)
    api_mo = a.api_est_monthly
    ov = max(0.0, float(overage_scale))
    pack = float(pack_usd) if pack_usd > 0 else DEFAULT_TOPOFF_PACK_USD
    # Continuous overage face from include math; card math uses pack rounding
    tops_raw = max(0.0, a.supergrok.overage_list_usd)
    tops = ceil_to_pack_usd(tops_raw, pack) if tops_raw > 0.5 else 0.0
    tops_hv_raw = max(0.0, a.heavy.overage_list_usd)
    tops_hv = ceil_to_pack_usd(tops_hv_raw, pack) if tops_hv_raw > 0.5 else 0.0
    # SuperGrok/Heavy full monthly with pack-rounded tops
    sg_full_mo = a.supergrok.sub_usd + tops
    hv_full_mo = a.heavy.sub_usd + tops_hv

    scenarios = [
        d
        for d in (
            topoff_scenarios
            if topoff_scenarios is not None
            else list(DEFAULT_TOPOFF_DISCOUNT_SCENARIOS)
        )
        if d > 0
    ]
    # Candidates: (key, label, monthly, is_promo, discount)
    cands: list[tuple[str, str, float, bool, float | None]] = []
    cands.append(("api", "Pure API", api_mo, False, None))
    cands.append(
        ("sg_full", f"SuperGrok ${a.supergrok.sub_usd:g}", sg_full_mo, False, None)
    )
    cands.append(
        ("hv_full", f"Heavy ${a.heavy.sub_usd:g}", hv_full_mo, False, None)
    )
    for d in scenarios:
        sg_c = a.supergrok.sub_usd + tops * (1.0 - d)
        pct = int(round(d * 100))
        cands.append((f"sg_{pct}", f"SuperGrok @ −{pct}% tops", sg_c, True, d))
        if tops_hv > 0.5:
            hv_c = a.heavy.sub_usd + tops_hv * (1.0 - d)
            cands.append((f"hv_{pct}", f"Heavy @ −{pct}% tops", hv_c, True, d))

    win_key, win_label, win_mo, win_promo, win_d = min(cands, key=lambda c: c[2])
    full_cands = [c for c in cands if not c[3]]
    _full_key, full_label, full_mo, _, _ = min(full_cands, key=lambda c: c[2])

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

    # Context (not summands): weekly pool est vs pack-rounded tops face
    pack_note = ""
    if tops > 0.5 and abs(tops - tops_raw) > 0.5:
        pack_note = f" · packs ${pack:g}: ${tops_raw:.0f}→${tops:.0f}"
    elif tops > 0.5:
        pack_note = f" · packs ${pack:g}"
    sg_note = (
        f"$/mo = sub ${a.supergrok.sub_usd:g}"
        + (f" + pack tops ${tops:.0f}@list" if tops > 0.5 else "")
        + f"  [dim]| pool ~${a.supergrok.weekly_include_usd:g}/wk est.[/dim]"
        + pack_note
    )
    if tops > a.supergrok.sub_usd * 2:
        sg_note += " · tops dominate"
    hv_note = (
        f"$/mo = sub ${a.heavy.sub_usd:g}"
        + (f" + pack tops ${tops_hv:.0f}" if tops_hv > 0.5 else " · tops rare")
        + f"  [dim]| pool ~${a.heavy.weekly_include_usd:g}/wk est.[/dim]"
    )
    _plan_row(
        t,
        option=f"SuperGrok ${a.supergrok.sub_usd:g}",
        monthly=sg_full_mo,
        notes=sg_note,
        highlight=win_key == "sg_full",
    )
    _plan_row(
        t,
        option=f"Heavy ${a.heavy.sub_usd:g}",
        monthly=hv_full_mo,
        notes=hv_note,
        highlight=win_key == "hv_full",
    )

    for key, lab, monthly, is_promo, d in cands:
        if not is_promo or d is None:
            continue
        pin = abs(d - active_d) < 1e-9 and active_d > 0
        note = f"$/mo = sub + pack tops×{1.0 - d:g}"
        if pin:
            note += "  ← your --topoff-discount"
        if key.startswith("hv_"):
            note = "$/mo = sub + pack tops (promo)"
            if pin:
                note += "  ← your --topoff-discount"
        _plan_row(
            t,
            option=lab,
            monthly=monthly,
            notes=note,
            highlight=key == win_key,
        )

    console.print(t)

    # Single best-plan blurb — promo dependency in the headline when relevant
    save_api = api_mo - win_mo
    if win_promo and win_d is not None:
        pct = int(round(win_d * 100))
        console.print(
            f"[bold green]★ Best plan for this window’s usage "
            f"(if −{pct}% pack promo holds):[/bold green] "
            f"[bold]{win_label}[/bold] ~${win_mo:.0f}/mo"
            + (f"  (~${save_api:.0f}/mo under Pure API)" if save_api > 0.5 else "")
        )
        console.print(
            f"  [dim]If promo ends: {full_label} ~${full_mo:.0f}/mo "
            f"(not the promo row).[/dim]"
        )
    elif win_key == "api":
        console.print(
            f"[bold green]★ Best plan for this window’s usage:[/bold green] "
            f"[bold]{win_label}[/bold] ~${win_mo:.0f}/mo"
        )
    else:
        console.print(
            f"[bold green]★ Best plan for this window’s usage "
            f"(full-price tops):[/bold green] "
            f"[bold]{win_label}[/bold] ~${win_mo:.0f}/mo"
            + (f"  (~${save_api:.0f}/mo under Pure API)" if save_api > 0.5 else "")
        )

    # Compact one-liner always; deep notes only with --detail
    console.print(
        f"[dim]~${a.daily_list:.2f} list$/day · {fmt_tokens(int(a.daily_tokens))} tok/day"
        f" · pack tops ${pack:g}"
        f" · promos temporary · quieter months → Pure API safer"
        f"{' · --detail for breakdowns' if not detail else ''}[/dim]"
    )
    _print_auth_now_hint(auth_line)

    if not detail:
        return

    # --- --detail: attribution, 1.9× source, weeks, hybrid, per-app regime ---
    if win_promo and win_d is not None:
        promo_part = tops * win_d
        structure_part = api_mo - sg_full_mo
        console.print(
            f"  [dim]Savings vs Pure API ~${save_api:.0f}/mo ≈ "
            f"${structure_part:.0f} plan structure (sub+include vs all list$) "
            f"+ ${promo_part:.0f} promo on pack tops "
            f"(sub ${a.supergrok.sub_usd:g} + ${tops:.0f}×{1.0 - win_d:g}"
            + (
                f"; need ${tops_raw:.0f}→${tops:.0f} @ ${pack:g} packs"
                if abs(tops - tops_raw) > 0.5
                else ""
            )
            + ").[/dim]"
        )
    elif save_api > 0.5:
        structure_part = api_mo - win_mo
        console.print(
            f"  [dim]Savings vs Pure API ~${structure_part:.0f}/mo from "
            f"subscription + included-pool model (tops@list pack face), "
            f"not from a measured weekly pool $.[/dim]"
        )

    if tops > 0.5 and ov > 1.01:
        tops_hi = ceil_to_pack_usd(tops_raw * ov, pack)
        sg_full_hi = a.supergrok.sub_usd + tops_hi
        best_promo_hi = min(
            (a.supergrok.sub_usd + tops_hi * (1.0 - d), d) for d in scenarios
        )
        hi_mo, hi_d = best_promo_hi
        hi_pct = int(round(hi_d * 100))
        console.print(
            f"  [dim]If Extra Credits burn ~{ov:g}× list$ "
            f"(config cash_scale_supergrok_overage, default {DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE:g}; "
            f"measured face/list$ on overage windows, not from this table): "
            f"SuperGrok full ~${sg_full_hi:.0f}/mo; "
            f"best promo −{hi_pct}% ~${hi_mo:.0f}/mo; "
            f"Heavy ~${hv_full_mo:.0f}/mo"
            + (
                " → Heavy wins if promo ends and burn stays high"
                if hi_mo > hv_full_mo and sg_full_hi > hv_full_mo
                else ""
            )
            + ".[/dim]"
        )

    if mix_slices:
        paths = {getattr(s, "path", "") for s in mix_slices}
        has_api = "api_key" in paths
        has_sg = any(p.startswith("supergrok") for p in paths if isinstance(p, str))
        if has_api and has_sg:
            console.print(
                "  [dim]Hybrid tip: window already mixes API + SuperGrok — "
                "route heavy agent/Build to API key; SuperGrok for interactive.[/dim]"
            )

    if week_list and len(week_list) >= 1:
        bits = [f"{wk} ${usd:.0f}" for wk, usd in week_list]
        vals = [usd for _, usd in week_list]
        lo, hi = min(vals), max(vals)
        spread = (
            f"  range ${lo:.0f}–${hi:.0f}/wk"
            if len(vals) > 1 and hi - lo > 1
            else ""
        )
        console.print(
            f"  [dim]Week list$ (pace check): {' · '.join(bits)}{spread}. "
            f"Point estimate assumes this pace holds.[/dim]"
        )

    if list_by_key_path:
        _print_per_app_regime(list_by_key_path, top_n=8)

    face = a.api_list_monthly * ov
    console.print(
        f"  [dim]Weekly pool $ is estimated (heavy Build often exhausts early). "
        f"Overage lens all@×{ov:g}: ~${face:.0f}/mo face before packs/promo. "
        f"Spend can be burstier than the monthly average.[/dim]"
    )


def _print_per_app_regime(
    list_by_key_path: dict[str, dict[str, float]],
    *,
    top_n: int = 8,
) -> None:
    """Per-app list$ share by regime (not the same as 'subscription savings')."""
    path_labs = {
        "api_key": "API",
        "supergrok_pool": "pool",
        "supergrok_overage": "overage",
        "supergrok_unknown": "SG(?)",
        "supergrok_session": "SG",
        "unknown": "?",
        "uniform": "uniform",
    }
    rows: list[tuple[str, float, dict[str, float]]] = []
    for key, paths in list_by_key_path.items():
        tot = sum(paths.values())
        if tot <= 0:
            continue
        rows.append((key, tot, paths))
    rows.sort(key=lambda r: -r[1])
    if not rows:
        return
    console.print(
        "  [dim]Per-app regime (list$ share · SG(?) = weekly% unknown → list$ scale, "
        "not 'no pool benefit'):[/dim]"
    )
    for key, tot, paths in rows[:top_n]:
        names = list(paths.keys())
        weights = [paths[n] for n in names]
        pcts = _reconcile_pcts(weights)
        bits = [
            f"{path_labs.get(n, n)} {p}%"
            for n, p in zip(names, pcts, strict=True)
            if p > 0
        ]
        short = key if len(key) <= 36 else key[:35] + "…"
        console.print(f"    [dim]{short}: list${tot:.0f}  {' · '.join(bits)}[/dim]")


def plan_advisor_export(
    a: PlanAdvisorResult,
    *,
    topoff_scenarios: list[float] | None = None,
    active_topoff_discount: float = 0.0,
    pack_usd: float = 100.0,
    overage_scale: float = 1.9,
    week_list: list[tuple[str, float]] | None = None,
    list_by_key_path: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Structured plan-advisor view for --json (mirrors human table scoring)."""
    from .pricing import (
        DEFAULT_TOPOFF_DISCOUNT_SCENARIOS,
        DEFAULT_TOPOFF_PACK_USD,
        ceil_to_pack_usd,
    )

    active_d = float(active_topoff_discount or 0.0)
    pack = float(pack_usd) if pack_usd > 0 else DEFAULT_TOPOFF_PACK_USD
    tops_raw = max(0.0, a.supergrok.overage_list_usd)
    tops = ceil_to_pack_usd(tops_raw, pack) if tops_raw > 0.5 else 0.0
    tops_hv_raw = max(0.0, a.heavy.overage_list_usd)
    tops_hv = ceil_to_pack_usd(tops_hv_raw, pack) if tops_hv_raw > 0.5 else 0.0
    sg_full_mo = a.supergrok.sub_usd + tops
    hv_full_mo = a.heavy.sub_usd + tops_hv
    api_mo = a.api_est_monthly
    scenarios = [
        d
        for d in (
            topoff_scenarios
            if topoff_scenarios is not None
            else list(DEFAULT_TOPOFF_DISCOUNT_SCENARIOS)
        )
        if d > 0
    ]
    candidates: list[dict[str, Any]] = [
        {"id": "api", "label": "Pure API", "monthly": round(api_mo, 4), "promo": False},
        {
            "id": "sg_full",
            "label": f"SuperGrok ${a.supergrok.sub_usd:g}",
            "monthly": round(sg_full_mo, 4),
            "promo": False,
            "pack_tops_face": tops,
            "tops_face_raw": tops_raw,
        },
        {
            "id": "hv_full",
            "label": f"Heavy ${a.heavy.sub_usd:g}",
            "monthly": round(hv_full_mo, 4),
            "promo": False,
        },
    ]
    for d in scenarios:
        pct = int(round(d * 100))
        candidates.append(
            {
                "id": f"sg_{pct}",
                "label": f"SuperGrok @ −{pct}% tops",
                "monthly": round(a.supergrok.sub_usd + tops * (1.0 - d), 4),
                "promo": True,
                "discount": d,
            }
        )
    best = min(candidates, key=lambda c: c["monthly"])
    full_best = min(
        (c for c in candidates if not c.get("promo")), key=lambda c: c["monthly"]
    )
    out: dict[str, Any] = {
        "window_days": a.window_days,
        "project_days": a.project_days,
        "pack_usd": pack,
        "tops_face_raw": round(tops_raw, 4),
        "tops_face_pack_ceil": round(tops, 4),
        "candidates": candidates,
        "best": {
            **best,
            "depends_on_promo": bool(best.get("promo")),
            "save_vs_api": round(api_mo - best["monthly"], 4),
        },
        "full_price_fallback": full_best,
        "overage_scale": ov if (ov := max(0.0, float(overage_scale))) else 1.9,
        "overage_scale_source": (
            f"cash_scale_supergrok_overage (default {DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE:g}; "
            "measured Extra Credit face/list$ on overage windows)"
        ),
        "week_list_usd": (
            [{"week": w, "list_usd": round(u, 4)} for w, u in week_list]
            if week_list
            else []
        ),
        "list_by_key_path": list_by_key_path or {},
        "assumptions": {
            "table_tops_at_list_face_pack_ceil": True,
            "weekly_include_usd_estimated": True,
            "promo_temporary": True,
        },
    }
    if best.get("promo") and best.get("discount") is not None:
        d = float(best["discount"])
        out["best"]["savings_vs_api"] = {
            "total": round(api_mo - best["monthly"], 4),
            "plan_structure_vs_pure_api": round(api_mo - sg_full_mo, 4),
            "promo_on_pack_tops": round(tops * d, 4),
        }
    return out


def _print_auth_now_hint(auth_line: str | None) -> None:
    """Print plan-advisor auth scope line (already plain-language from auth_status)."""
    if not auth_line:
        return
    console.print(f"[dim]{auth_line}[/dim]")


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
    "plan_advisor_export",
    "print_plan_advisor",
    "print_token_cost_summary",
    "print_wallet_auth_line",
    "week_list_series",
]
