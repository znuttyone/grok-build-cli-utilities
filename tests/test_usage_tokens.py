"""Tests for token-accurate usage parsing and cost estimation."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from grok_build_cli_utilities.cli import app
from grok_build_cli_utilities.utils.auth_status import format_weekly_reset_local
from grok_build_cli_utilities.utils.pricing import (
    DEFAULT_CASH_SCALE,
    DEFAULT_CASH_SCALE_API,
    DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE,
    DEFAULT_CASH_SCALE_SUPERGROK_POOL,
    DEFAULT_HEAVY_USD,
    DEFAULT_HEAVY_WEEKLY_INCLUDE_USD,
    DEFAULT_SUPERGROK_USD,
    api_estimate_usd,
    effective_rates,
    plan_advisor,
    rates_for_model,
    resolve_cash_scale,
)
from grok_build_cli_utilities.utils.usage_tokens import (
    PaygoTypeUsd,
    aggregate,
    allocate_invoice,
    allocate_paygo,
    allocate_paygo_by_type,
    filter_usage,
    load_turn_usage,
    total_bucket,
)

runner = CliRunner()


def _json_from_cli(result) -> dict:
    """Parse JSON from CLI result; ignore stderr warnings mixed into output."""
    text = result.stdout or result.output or ""
    start = text.find("{")
    assert start >= 0, f"no JSON in output: {text[:200]!r}"
    return json.loads(text[start:])


def _write_turn(
    path: Path,
    *,
    prompt_id: str,
    ts: str,
    input_t: int,
    output_t: int,
    cached: int,
    reasoning: int,
    ticks: int,
    model: str = "grok-build-0.1",
    total: int | None = None,
) -> None:
    tot = total if total is not None else input_t + output_t
    usage = {
        "inputTokens": input_t,
        "outputTokens": output_t,
        "totalTokens": tot,
        "cachedReadTokens": cached,
        "reasoningTokens": reasoning,
        "costUsdTicks": ticks,
        "modelCalls": 1,
        "modelUsage": {
            model: {
                "inputTokens": input_t,
                "outputTokens": output_t,
                "totalTokens": tot,
                "cachedReadTokens": cached,
                "reasoningTokens": reasoning,
                "costUsdTicks": ticks,
                "modelCalls": 1,
            }
        },
    }
    obj = {
        "method": "session/update",
        "timestamp": ts,
        "params": {
            "sessionId": path.parent.name,
            "update": {
                "sessionUpdate": "turn_completed",
                "prompt_id": prompt_id,
                "usage": usage,
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def test_list_price_usd_matches_xai_ticks():
    from grok_build_cli_utilities.utils.usage_tokens import (
        TICKS_PER_USD,
        list_price_usd,
        turn_list_usd,
    )
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec
    from datetime import datetime, timezone

    assert TICKS_PER_USD == 10_000_000_000
    # update-for-46 /usage Cost $1.5465
    assert abs(list_price_usd(15_464_980_000) - 1.546498) < 1e-9
    # ProfitGuard /usage Cost $77.3463 (mid-session snapshot)
    assert abs(list_price_usd(773_463_000_000) - 77.3463) < 1e-9
    rec = UsageRec(
        prompt_id="t",
        ts=datetime(2026, 8, 13, tzinfo=timezone.utc),
        project="P",
        cwd="/P",
        session_id="s",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
        ticks=15_464_980_000,
    )
    assert abs(turn_list_usd(rec) - 1.546498) < 1e-9
    # -m / prefer_ticks=False reconstructs 4.6 cached $0.50
    assert abs(turn_list_usd(rec, rates_for_model("grok-4.6"), prefer_ticks=False) - 0.50) < 1e-9


def test_api_estimate_math():
    # 1M cached @ 0.20 + 1M uncached @ 1.00 + 0.5M out @ 2.00 = 0.2+1+1 = 2.2
    est = api_estimate_usd(
        cached=1_000_000,
        uncached_in=1_000_000,
        output=500_000,
        reasoning=0,
        rates=rates_for_model("grok-build"),
    )
    assert abs(est - 2.2) < 1e-9


def test_rates_for_model_fuzzy():
    from grok_build_cli_utilities.utils.pricing import (
        DEFAULT_RATES_MODEL,
        resolve_rates_model,
    )

    r = rates_for_model("grok-4.5-build")
    # Matches console grok-4.5 Pricing (≤200k): input $2 / cached $0.30 / output $6
    assert r.cached_input == 0.30
    assert r.uncached_input == 2.00
    assert r.output == 6.00
    r46 = rates_for_model("grok-4.6")
    # docs.x.ai 2026-08-13: $2 / $0.50 / $6 (cached is the 4.5→4.6 list-rate delta)
    assert r46.label == "grok-4.6"
    assert r46.cached_input == 0.50
    assert r46.uncached_input == 2.00
    assert r46.output == 6.00
    r2 = rates_for_model("unknown-xyz")
    assert r2.uncached_input == 2.00  # default is grok-4.6-class
    assert r2.cached_input == 0.50
    assert DEFAULT_RATES_MODEL == "grok-4.6"
    r3 = rates_for_model("grok-build-0.1")
    assert r3.cached_input == 0.20
    label, rates = resolve_rates_model("4.5")
    assert label == "grok-4.5"
    assert rates.output == 6.00
    label46, rates46 = resolve_rates_model("4.6")
    assert label46 == "grok-4.6"
    assert rates46.cached_input == 0.50
    _label2, rates2 = resolve_rates_model("build")
    assert rates2.cached_input == 0.20
    # grok-4 must not steal grok-4.6* (old substring match billed $3/$15)
    for alias in ("grok-4.6", "4.6", "grok-4.6-build", "grok-4.6-fast"):
        got = rates_for_model(alias)
        assert got.label == "grok-4.6", alias
        assert got.cached_input == 0.50, alias
        assert got.output == 6.00, alias
    assert rates_for_model("grok-4").output == 15.00
    assert rates_for_model("grok-4-0709").output == 15.00


def test_load_dedupe_and_filter(tmp_path: Path):
    # encoded cwd style path
    sess = tmp_path / "sessions" / "%2FUsers%2Fme%2FDocuments%2FGitHub%2FBlessed-Bits" / "sid1"
    upd = sess / "updates.jsonl"
    _write_turn(
        upd,
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=1000,
        cached=900_000,
        reasoning=100,
        ticks=1_000_000_000,
    )
    # lower total same prompt — should be ignored
    _write_turn(
        upd,
        prompt_id="p1",
        ts="2026-08-02T12:01:00Z",
        input_t=100,
        output_t=1,
        cached=0,
        reasoning=0,
        ticks=1,
        total=50,
    )
    # out of range
    _write_turn(
        upd,
        prompt_id="p2",
        ts="2026-07-01T12:00:00Z",
        input_t=10,
        output_t=1,
        cached=0,
        reasoning=0,
        ticks=1,
    )
    # second prompt in range
    _write_turn(
        upd,
        prompt_id="p3",
        ts="2026-08-03T12:00:00Z",
        input_t=500_000,
        output_t=500,
        cached=400_000,
        reasoning=50,
        ticks=500_000_000,
    )

    recs = load_turn_usage(tmp_path / "sessions")
    assert len(recs) == 3  # p1 kept max, p2, p3
    filtered = filter_usage(recs, date_from=date(2026, 8, 1), date_to=date(2026, 8, 5))
    assert len(filtered) == 2
    assert all(r.project == "Blessed-Bits" for r in filtered)

    buckets = aggregate(filtered, "app")
    assert len(buckets) == 1
    b = buckets[0]
    assert b.n == 2
    assert b.cached == 1_300_000
    tot = total_bucket(filtered)
    assert tot.api_est() > 0


def test_invoice_allocation(tmp_path: Path):
    sess = tmp_path / "sessions" / "projA" / "s1"
    upd = sess / "updates.jsonl"
    _write_turn(
        upd,
        prompt_id="a",
        ts="2026-08-01T00:00:00Z",
        input_t=1000,
        output_t=10,
        cached=0,
        reasoning=0,
        ticks=100,
    )
    sess2 = tmp_path / "sessions" / "projB" / "s2"
    upd2 = sess2 / "updates.jsonl"
    _write_turn(
        upd2,
        prompt_id="b",
        ts="2026-08-01T01:00:00Z",
        input_t=1000,
        output_t=10,
        cached=0,
        reasoning=0,
        ticks=300,
    )
    recs = load_turn_usage(tmp_path / "sessions")
    buckets = aggregate(recs, "app")
    scale, fixed_per = allocate_invoice(
        buckets,
        invoice_usd=40.0,
        fixed_usd=8.0,
        group="app",
    )
    # scale = 40/400 = 0.1
    assert abs(scale - 0.1) < 1e-9
    assert abs(fixed_per - 4.0) < 1e-9  # 8/2 buckets
    by_key = {b.key: b for b in buckets}
    # proj names from path
    vars_ = {k: b.ticks * scale for k, b in by_key.items()}
    assert abs(sum(vars_.values()) - 40.0) < 1e-6


def test_usage_cost_cli_json(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "%2Ftmp%2FMyApp" / "s1"
    upd = sess / "updates.jsonl"
    _write_turn(
        upd,
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=2000,
        cached=800_000,
        reasoning=500,
        ticks=2_000_000_000,
        model="grok-build-0.1",
    )
    # summary so other commands still work if needed
    (sess / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": "s1", "cwd": "/tmp/MyApp"},
                "created_at": "2026-08-02T12:00:00Z",
                "num_messages": 2,
                "current_model_id": "grok-build-0.1",
            }
        )
    )

    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-05",
            "--by",
            "app",
            "--invoice-usd",
            "10",
            "--fixed-usd",
            "2",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["mode"] == "tokens"
    assert data["totals"]["prompts"] == 1
    assert data["totals"]["list_usd"] > 0 or data["totals"].get("api_est_usd", 0) > 0
    assert data["invoice"]["invoice_usd"] == 10
    assert data["totals"]["est_usd"] >= 0


def test_usage_cost_rough_compat(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "p" / "s1"
    sess.mkdir(parents=True)
    (sess / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": "s1", "cwd": "/p"},
                "created_at": "2026-06-01T00:00:00Z",
                "num_messages": 10,
                "current_model_id": "grok-3",
            }
        )
    )
    r = runner.invoke(
        app, ["-g", str(grok), "usage", "cost", "--mode", "rough", "--by", "model", "--json"]
    )
    assert r.exit_code == 0
    assert "rough" in r.output
    assert "estimated_total_usd" in r.output


def test_usage_report_tokens(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppX" / "s1"
    upd = sess / "updates.jsonl"
    _write_turn(
        upd,
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=100_000,
        output_t=100,
        cached=50_000,
        reasoning=10,
        ticks=1000,
    )
    r = runner.invoke(app, ["-g", str(grok), "usage", "report", "--by", "app", "--json"])
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["mode"] == "tokens"
    assert data["totals"]["prompts"] == 1
    # Redundant --tokens with --by app should warn but still succeed
    r2 = runner.invoke(
        app, ["-g", str(grok), "usage", "report", "--tokens", "--by", "app", "--json"]
    )
    assert r2.exit_code == 0, r2.output
    assert "ignored" in (r2.output + r2.stderr).lower()


def test_usage_info():
    r = runner.invoke(app, ["usage", "info"])
    assert r.exit_code == 0
    assert "SuperGrok" in r.output


def test_allocate_paygo_sums_to_total(tmp_path: Path):
    sess = tmp_path / "sessions" / "A" / "s1"
    upd = sess / "updates.jsonl"
    _write_turn(
        upd,
        prompt_id="a",
        ts="2026-08-01T00:00:00Z",
        input_t=1000,
        output_t=10,
        cached=0,
        reasoning=0,
        ticks=100,
    )
    sess2 = tmp_path / "sessions" / "B" / "s2"
    _write_turn(
        sess2 / "updates.jsonl",
        prompt_id="b",
        ts="2026-08-01T01:00:00Z",
        input_t=1000,
        output_t=10,
        cached=0,
        reasoning=0,
        ticks=300,
    )
    recs = load_turn_usage(tmp_path / "sessions")
    buckets = aggregate(recs, "app")
    alloc = allocate_paygo(buckets, paygo_usd=5.43, weight="ticks")
    assert abs(sum(alloc.values()) - 5.43) < 1e-9
    # 100:300 → 25%:75%
    assert abs(max(alloc.values()) - 5.43 * 0.75) < 1e-6


def test_usage_cost_est_cash_scale_json(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppY" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=1000,
        cached=500_000,
        reasoning=100,
        ticks=1_000_000_000,
    )
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-04",
            "--by",
            "app",
            "--rates-model",
            "grok-4.5",
            "--cash-scale",
            "0.48",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["rates_model"] == "grok-4.5"
    assert data["cash_scale"] == 0.48
    assert data["rates"]["cached_input"] == 0.3
    list_t = data["totals"]["list_usd"]
    est_t = data["totals"]["est_usd"]
    assert abs(est_t - list_t * 0.48) < 1e-3
    # effective = list × scale
    assert abs(data["effective_rates"]["uncached_input"] - 2.0 * 0.48) < 1e-6
    assert abs(data["effective_rates"]["cached_input"] - 0.3 * 0.48) < 1e-6
    assert abs(data["effective_rates"]["output"] - 6.0 * 0.48) < 1e-6


def test_effective_rates_uniform_scale():
    r = rates_for_model("grok-4.5")
    eff = effective_rates(r, 0.5656)
    assert abs(eff.uncached_input - 2.0 * 0.5656) < 1e-9
    assert abs(eff.cached_input - 0.3 * 0.5656) < 1e-9
    assert abs(eff.output - 6.0 * 0.5656) < 1e-9
    assert DEFAULT_CASH_SCALE == DEFAULT_CASH_SCALE_API == 1.0
    assert DEFAULT_CASH_SCALE_SUPERGROK_POOL == 0.0
    assert DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE == 1.9


def test_resolve_cash_scale_by_auth_path():
    s, src = resolve_cash_scale(auth_effective="api_key")
    assert s == 1.0
    assert "api_key" in src
    s, src = resolve_cash_scale(auth_effective="supergrok_session", weekly_usage_pct=10.0)
    assert s == 0.0
    assert "pool" in src
    s, src = resolve_cash_scale(auth_effective="supergrok_session", weekly_usage_pct=100.0)
    assert s == 1.9
    assert "overage" in src
    s, src = resolve_cash_scale(auth_effective="supergrok_session", weekly_usage_pct=None)
    assert s == 1.0
    assert "unknown" in src


def test_build_token_cost_window_est_by_key(tmp_path: Path):
    """Shared builder: one-pass est_by_key matches totals; regimes split."""
    from datetime import datetime, timezone

    from grok_build_cli_utilities.utils.auth_status import AuthHistoryEvent
    from grok_build_cli_utilities.utils.pricing import estimate_with_auth_mix, rates_for_model
    from grok_build_cli_utilities.utils.usage_cost_window import build_token_cost_window
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec, load_turn_usage

    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppX" / "s1"
    sess.mkdir(parents=True)
    (grok / "auth.json").write_text('{"access_token": "' + "x" * 40 + '"}', encoding="utf-8")
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-07T12:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=1,
        total=1_000_000,
    )
    records = load_turn_usage(grok / "sessions")
    win = build_token_cost_window(grok, records, group="app", rates_model="grok-4.5")
    assert win.list_total > 0
    assert win.est_by_key
    assert abs(sum(win.est_by_key.values()) - win.est_total) < 1e-6

    # Regime split: pool + overage as separate paths
    rates = rates_for_model("grok-4.5")
    r_pool = UsageRec(
        prompt_id="pool",
        ts=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    r_ov = UsageRec(
        prompt_id="ov",
        ts=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    pts = [AuthHistoryEvent("2026-08-01T00:00:00+00:00", "cached_token", "sel")]
    weekly_tl = [
        (datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 8, 7, 11, 0, tzinfo=timezone.utc), 10.0),
    ]
    mix = estimate_with_auth_mix(
        [r_pool, r_ov],
        rates,
        change_points=pts,
        weekly_timeline=weekly_tl,
        fallback_auth="supergrok_session",
        group_key_fn=lambda r: r.project,
    )
    paths = {s.path for s in mix.slices}
    assert "supergrok_pool" in paths
    assert "supergrok_overage" in paths
    assert abs(mix.est_by_key["A"] - mix.est_total) < 1e-6


def test_apply_topoff_discount_25_and_100():
    from grok_build_cli_utilities.utils.pricing import (
        apply_topoff_discount,
        ceil_to_pack_usd,
        resolve_topoff_discount,
        resolve_topoff_discount_scenarios,
        topoff_discount_label,
    )
    from grok_build_cli_utilities.utils.usage_display import _reconcile_pcts

    assert abs(apply_topoff_discount(100.0, 0.25) - 75.0) < 1e-9
    assert abs(apply_topoff_discount(100.0, 1.0) - 0.0) < 1e-9
    assert abs(apply_topoff_discount(100.0, 1.5) - 0.0) < 1e-9  # clamp to 1.0
    d, src = resolve_topoff_discount(cli_discount=1.0)
    assert d == 1.0
    assert "1" in src
    d, src = resolve_topoff_discount({"topoff_discount": 0.25})
    assert abs(d - 0.25) < 1e-9
    scenarios = resolve_topoff_discount_scenarios(None)
    assert scenarios == [0.20, 0.25, 0.40]
    assert 0.0 not in scenarios and 1.0 not in scenarios
    scenarios = resolve_topoff_discount_scenarios(
        {"topoff_discount_scenarios": [0.0, 0.4]}, active_discount=0.25
    )
    # 0.0 dropped (full price is base row); 0.4 + active 0.25 kept
    assert 0.0 not in scenarios and 0.4 in scenarios and 0.25 in scenarios
    assert "40" in topoff_discount_label(0.40) or "promo" in topoff_discount_label(0.40)
    # Pack rounding: $380 face → $400 @ $100 packs
    assert abs(ceil_to_pack_usd(380.0, 100.0) - 400.0) < 1e-9
    assert abs(ceil_to_pack_usd(100.0, 100.0) - 100.0) < 1e-9
    assert abs(ceil_to_pack_usd(0.0, 100.0) - 0.0) < 1e-9
    # Regime % always sum to 100
    assert sum(_reconcile_pcts([93.4, 5.2, 1.4])) == 100
    assert sum(_reconcile_pcts([1.0, 1.0, 1.0])) == 100


def test_estimate_with_auth_mix_splits_paths():
    from datetime import datetime, timezone

    from grok_build_cli_utilities.utils.auth_status import AuthHistoryEvent
    from grok_build_cli_utilities.utils.pricing import estimate_with_auth_mix
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec

    rates = rates_for_model("grok-4.5")
    # 1M cached only → list$ 0.30 each
    r_sg = UsageRec(
        prompt_id="a",
        ts=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s1",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    r_api = UsageRec(
        prompt_id="b",
        ts=datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s1",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    pts = [
        AuthHistoryEvent("2026-08-06T10:00:00+00:00", "cached_token", "sel"),
        AuthHistoryEvent("2026-08-07T15:00:00+00:00", "xai.api_key", "sel"),
    ]
    # Per-turn weekly timeline: SuperGrok hour at 100% overage, not current 10%
    weekly_tl = [
        (datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc), 100.0),
        (datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc), 10.0),
    ]
    mix = estimate_with_auth_mix(
        [r_sg, r_api],
        rates,
        change_points=pts,
        weekly_usage_pct=10.0,  # "current" — must NOT zero historical SuperGrok
        weekly_timeline=weekly_tl,
        fallback_auth="supergrok_session",
    )
    assert mix.source == "auth_mix"
    by_path = {s.path: s for s in mix.slices}
    # SuperGrok at 100% weekly → overage slice (regime-split path)
    assert "supergrok_overage" in by_path
    assert "api_key" in by_path
    assert abs(by_path["supergrok_overage"].est_usd - 0.30 * 1.9) < 1e-6
    assert abs(by_path["api_key"].est_usd - 0.30) < 1e-6
    assert abs(mix.est_total - (0.30 * 1.9 + 0.30)) < 1e-6


def test_historical_supergrok_without_weekly_uses_list_unknown():
    """Before an *overage* first sample, SuperGrok must not invent pool 0 or 1.9."""
    from datetime import datetime, timezone

    from grok_build_cli_utilities.utils.auth_status import AuthHistoryEvent
    from grok_build_cli_utilities.utils.pricing import estimate_with_auth_mix
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec

    rates = rates_for_model("grok-4.5")
    r = UsageRec(
        prompt_id="old",
        ts=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s1",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    pts = [AuthHistoryEvent("2026-08-01T00:00:00+00:00", "cached_token", "sel")]
    # weekly timeline only starts Aug 6; Aug 3 has no sample → unknown@list 1.0
    weekly_tl = [(datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc), 100.0)]
    mix = estimate_with_auth_mix(
        [r],
        rates,
        change_points=pts,
        weekly_usage_pct=8.0,
        weekly_timeline=weekly_tl,
        fallback_auth="supergrok_session",
    )
    assert abs(mix.est_total - 0.30 * 1.0) < 1e-6
    assert mix.slices and mix.slices[0].path == "supergrok_unknown"


def test_pre_log_in_pool_heavy_uses_pool_not_list():
    """Truncated billing log: first sample still in-pool → earlier same-week Heavy is pool 0."""
    from datetime import datetime, timezone

    from grok_build_cli_utilities.utils.auth_status import AuthHistoryEvent
    from grok_build_cli_utilities.utils.pricing import estimate_with_auth_mix
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec

    rates = rates_for_model("grok-4.5")
    r = UsageRec(
        prompt_id="old",
        ts=datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc),
        project="ProfitGuard",
        cwd="/ProfitGuard",
        session_id="s1",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    pts = [AuthHistoryEvent("2026-08-01T00:00:00+00:00", "cached_token", "sel")]
    weekly_tl = [(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), 15.0)]
    tier_tl = [(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), "heavy")]
    mix = estimate_with_auth_mix(
        [r],
        rates,
        change_points=pts,
        weekly_usage_pct=45.0,
        weekly_timeline=weekly_tl,
        tier_timeline=tier_tl,
        fallback_auth="supergrok_session",
    )
    assert mix.slices and mix.slices[0].path == "heavy_pool"
    assert mix.est_total == 0.0


def test_auth_mix_labels_heavy_pool_from_tier_timeline():
    from datetime import datetime, timezone

    from grok_build_cli_utilities.utils.auth_status import AuthHistoryEvent
    from grok_build_cli_utilities.utils.pricing import estimate_with_auth_mix
    from grok_build_cli_utilities.utils.usage_tokens import UsageRec

    rates = rates_for_model("grok-4.5")
    r = UsageRec(
        prompt_id="hv",
        ts=datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc),
        project="A",
        cwd="/A",
        session_id="s1",
        input=1_000_000,
        cached=1_000_000,
        total=1_000_000,
    )
    pts = [AuthHistoryEvent("2026-08-01T00:00:00+00:00", "cached_token", "sel")]
    weekly_tl = [(datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc), 10.0)]
    tier_tl = [
        (datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc), "supergrok"),
        (datetime(2026, 8, 16, 13, 12, tzinfo=timezone.utc), "heavy"),
    ]
    mix = estimate_with_auth_mix(
        [r],
        rates,
        change_points=pts,
        weekly_usage_pct=10.0,
        weekly_timeline=weekly_tl,
        tier_timeline=tier_tl,
        fallback_auth="supergrok_session",
    )
    assert mix.slices and mix.slices[0].path == "heavy_pool"
    assert mix.est_total == 0.0


def test_plan_advisor_high_volume_prefers_heavy():
    # ~20d window, ~$358 list, scale 0.57 → est ~$204 (user sample intensity)
    a = plan_advisor(
        list_usd=357.64,
        est_usd=203.85,
        tokens=865_700_000,
        cache_pct=95.3,
        cash_scale=0.57,
        window_days=20,
        project_days=30,
    )
    assert a.api_list_monthly > 500
    assert 300 < a.api_est_monthly < 330
    # SuperGrok: small weekly include → large list overage
    assert a.supergrok.overage_list_usd > 100
    assert a.supergrok.monthly > DEFAULT_SUPERGROK_USD + 100
    # Heavy: large include → flat ~$300
    assert a.heavy.overage_list_usd < 1.0
    assert abs(a.heavy.monthly - DEFAULT_HEAVY_USD) < 1.0
    assert a.winner == "heavy"
    assert a.heavy_cheaper_than_list_api is True
    assert a.heavy_breakeven_tokens_monthly is not None
    assert a.heavy_breakeven_tokens_monthly > 0


def test_plan_advisor_low_volume_api_est_wins():
    a = plan_advisor(
        list_usd=20.0,
        est_usd=11.4,
        tokens=5_000_000,
        cache_pct=90.0,
        cash_scale=0.57,
        window_days=30,
        project_days=30,
    )
    assert a.api_list_monthly == 20.0
    assert a.supergrok.overage_list_usd == 0.0
    assert a.winner == "api_est"
    assert DEFAULT_HEAVY_WEEKLY_INCLUDE_USD == 150.0


def test_plan_advisor_export_mirrors_tui_promos_and_active_pin():
    """--json candidates match -P: Heavy promo rows when tops exist, active pin."""
    from grok_build_cli_utilities.utils.usage_display import plan_advisor_export

    # 2d window at ~$45.32 list$ → ~$680/mo (PR Heavy sample). Advisor Pure API
    # uses API-scale list$, not mixed est$. SuperGrok pack tops ~$600; Heavy still
    # has a pack so hv_* rows exist and ★ is Heavy @ −40% (not SuperGrok @ −40%).
    a = plan_advisor(
        list_usd=45.32,
        est_usd=45.32,
        tokens=279_700_000,
        cache_pct=98.0,
        cash_scale=1.0,
        window_days=2,
        project_days=30,
    )
    assert a.heavy.overage_list_usd > 0.5
    assert a.supergrok.overage_list_usd > 100

    out = plan_advisor_export(a, active_topoff_discount=0.25)
    by_id = {c["id"]: c for c in out["candidates"]}
    assert {
        "api",
        "sg_full",
        "hv_full",
        "sg_20",
        "sg_25",
        "sg_40",
        "hv_20",
        "hv_25",
        "hv_40",
    } <= set(by_id)
    for cid, c in by_id.items():
        assert "active" in c
        if cid in ("sg_25", "hv_25"):
            assert c["active"] is True
        else:
            assert c["active"] is False
    assert by_id["hv_full"]["pack_tops_face"] > 0.5
    assert out["best"]["id"] == "hv_40"

    low = plan_advisor(
        list_usd=20.0,
        est_usd=11.4,
        tokens=5_000_000,
        cache_pct=90.0,
        cash_scale=0.57,
        window_days=30,
        project_days=30,
    )
    low_out = plan_advisor_export(low, active_topoff_discount=0.0)
    low_ids = {c["id"] for c in low_out["candidates"]}
    assert "hv_full" in low_ids
    assert "hv_40" not in low_ids
    assert all(c["active"] is False for c in low_out["candidates"])


def test_usage_report_by_app_includes_est(tmp_path: Path):
    """Token report --by app shows list$ + est$ with cash_scale."""
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppR" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=1,
        total=1_000_000,
    )
    # list$ = 1M cached * 0.30 = 0.30; est at 0.5 = 0.15
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "report",
            "--by",
            "app",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-04",
            "-m",
            "grok-4.5",
            "--cash-scale",
            "0.5",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["mode"] == "tokens"
    assert data["cash_scale"] == 0.5
    assert abs(data["totals"]["list_usd"] - 0.30) < 1e-6
    assert abs(data["totals"]["est_usd"] - 0.15) < 1e-6
    assert abs(data["buckets"][0]["est_usd"] - 0.15) < 1e-6
    assert data["result_from"] == "2026-08-02"
    assert data["result_to"] == "2026-08-02"


def test_usage_cost_plan_advisor_json(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppPA" / "s1"
    # Two days of usage so window_days >= 2
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-01T12:00:00Z",
        input_t=10_000_000,
        output_t=1000,
        cached=9_000_000,
        reasoning=100,
        ticks=1,
    )
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p2",
        ts="2026-08-10T12:00:00Z",
        input_t=10_000_000,
        output_t=1000,
        cached=9_000_000,
        reasoning=100,
        ticks=1,
    )
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-10",
            "-m",
            "grok-4.5",
            "--plan-advisor",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    pa = data["plan_advisor"]
    assert pa is not None
    assert pa["window_days"] == 10
    assert pa["project_days"] == 30
    assert "candidates" in pa and "best" in pa
    ids = {c["id"] for c in pa["candidates"]}
    assert "api" in ids and "sg_full" in ids and "hv_full" in ids
    assert pa["best"]["id"] in ids
    assert data.get("wallet") is not None
    assert sum(s.get("list_pct") or 0 for s in data["auth_mix"]["slices"]) == 100


def test_usage_cost_detects_heavy_from_billing_log(tmp_path: Path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    sess = grok / "sessions" / "AppH" / "s1"
    (grok / "auth.json").write_text(
        json.dumps({"access_token": "x" * 40, "email": "u@example.com"}),
        encoding="utf-8",
    )
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "unified.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-08-16T13:12:24Z",
                "msg": "billing: fetched credits config",
                "ctx": {
                    "subscriptionTier": "SuperGrok Heavy",
                    "config": {
                        "creditUsagePercent": 16.0,
                        "prepaidBalance": {"val": 15208},
                        "currentPeriod": {
                            "type": "USAGE_PERIOD_TYPE_WEEKLY",
                            "start": "2026-08-13T23:08:01+00:00",
                            "end": "2026-08-20T23:08:01+00:00",
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-16T15:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=10_000_000_000,
        total=1_000_000,
    )
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-16",
            "--to",
            "2026-08-16",
            "-P",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["wallet"]["subscription_tier"] == "heavy"
    assert data["wallet"]["subscription_tier_label"] == "Heavy"
    assert data["wallet"]["weekly_resets_at"] is not None
    assert data["wallet"]["weekly_resets_at"].startswith("2026-08-20T23:08:01")
    assert data["plan_advisor"]["current_tier"] == "heavy"
    paths = {s["path"] for s in data["auth_mix"]["slices"]}
    assert "heavy_pool" in paths

    human = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-16",
            "--to",
            "2026-08-16",
            "-P",
        ],
    )
    assert human.exit_code == 0, human.output
    out = human.output
    assert "Heavy pool" in out or "auth Heavy session" in out
    assert "current plan" in out
    assert "what-if" in out
    assert "Plan from billing log: SuperGrok Heavy" in out
    reset = format_weekly_reset_local(datetime(2026, 8, 20, 23, 8, 1, tzinfo=timezone.utc))
    assert reset is not None
    reset_lines = [
        ln for ln in out.splitlines() if reset in ln and "Weekly Heavy pool resets" in ln
    ]
    assert reset_lines, out
    assert all("Extra Credits" not in ln for ln in reset_lines)


def test_usage_cost_from_before_data_warns(tmp_path: Path):
    """--from earlier than any session turn → warning with actual earliest date."""
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppEarly" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-03T12:00:00Z",
        input_t=1000,
        output_t=10,
        cached=0,
        reasoning=0,
        ticks=1,
    )
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-07-01",
            "--by",
            "app",
            "-m",
            "grok-4.5",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    # Warning on stderr; JSON on stdout
    combined = (r.stdout or "") + (r.stderr or "") + (r.output or "")
    assert "before earliest session data" in combined
    assert "2026-08-03" in combined
    data = _json_from_cli(r)
    assert data["from"] == "2026-07-01"
    assert data["data_earliest"] == "2026-08-03"
    assert data["result_from"] == "2026-08-03"


def test_usage_cost_prepaid_sets_scale(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppY" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=1,
        total=1_000_000,
    )
    # list$ at 4.5 = 1M cached * 0.30 = 0.30; burn=0.15 → scale=0.5
    r = runner.invoke(
        app,
        [
            "-g",
            str(grok),
            "usage",
            "cost",
            "--from",
            "2026-08-01",
            "--to",
            "2026-08-04",
            "-m",
            "grok-4.5",
            "--prepaid-usd",
            "1.0",
            "--credits-remaining",
            "0.85",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert abs(data["cash_scale"] - 0.5) < 1e-6
    assert abs(data["totals"]["est_usd"] - 0.15) < 1e-6
    assert abs(data["effective_rates"]["cached_input"] - 0.15) < 1e-6  # 0.30 × 0.5


def test_allocate_paygo_by_type():
    # Two apps: A is cache-heavy, B is uncached-heavy
    from grok_build_cli_utilities.utils.usage_tokens import UsageBucket

    a = UsageBucket(key="A", n=1, input=900, cached=800, output=10, reasoning=0, total=910)
    b = UsageBucket(key="B", n=1, input=200, cached=50, output=90, reasoning=10, total=300)
    types = PaygoTypeUsd(cached=3.28, input=1.76, output=0.25, reasoning=0.05)
    bill, detail = allocate_paygo_by_type([a, b], types)
    assert abs(sum(bill.values()) - types.total) < 1e-6
    # A has most cache → most of cached $
    assert detail["A"]["cached_usd"] > detail["B"]["cached_usd"]
    # B has more completion → more of output $
    assert detail["B"]["output_usd"] > detail["A"]["output_usd"]


def test_usage_cost_rates_model_switch(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppZ" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-02T12:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=0,
        total=1_000_000,
    )
    base = ["-g", str(grok), "usage", "cost", "--from", "2026-08-01", "--to", "2026-08-04"]
    r45 = runner.invoke(app, [*base, "-m", "grok-4.5", "--json"])
    r46 = runner.invoke(app, [*base, "-m", "grok-4.6", "--json"])
    r_default = runner.invoke(app, [*base, "--json"])
    r_build = runner.invoke(app, [*base, "-m", "grok-build-0.1", "--json"])
    assert r45.exit_code == 0 and r46.exit_code == 0
    assert r_default.exit_code == 0 and r_build.exit_code == 0
    d45 = _json_from_cli(r45)
    d46 = _json_from_cli(r46)
    ddef = _json_from_cli(r_default)
    db = _json_from_cli(r_build)
    # 1M cached, no ticks: 4.5 → $0.30; 4.6 / default fallback → $0.50; build → $0.20
    assert abs(d45["totals"]["list_usd"] - 0.30) < 1e-6
    assert abs(d46["totals"]["list_usd"] - 0.50) < 1e-6
    assert d46["rates_model"] == "grok-4.6"
    assert ddef["list_source"] == "rates"
    assert abs(ddef["totals"]["list_usd"] - 0.50) < 1e-6
    assert abs(db["totals"]["list_usd"] - 0.20) < 1e-6


def test_usage_cost_default_uses_ticks(tmp_path: Path):
    grok = tmp_path / ".grok"
    sess = grok / "sessions" / "AppT" / "s1"
    _write_turn(
        sess / "updates.jsonl",
        prompt_id="p1",
        ts="2026-08-13T12:00:00Z",
        input_t=1_000_000,
        output_t=0,
        cached=1_000_000,
        reasoning=0,
        ticks=15_464_980_000,  # /usage $1.546498
        total=1_000_000,
    )
    r = runner.invoke(
        app,
        ["-g", str(grok), "usage", "cost", "--from", "2026-08-13", "--json"],
    )
    assert r.exit_code == 0, r.output
    data = _json_from_cli(r)
    assert data["list_source"] == "ticks"
    assert abs(data["totals"]["list_usd"] - 1.5465) < 1e-4
    # -m still reconstructs and ignores ticks
    r_m = runner.invoke(
        app,
        ["-g", str(grok), "usage", "cost", "--from", "2026-08-13", "-m", "grok-4.6", "--json"],
    )
    d_m = _json_from_cli(r_m)
    assert d_m["list_source"] == "rates"
    assert abs(d_m["totals"]["list_usd"] - 0.50) < 1e-6
