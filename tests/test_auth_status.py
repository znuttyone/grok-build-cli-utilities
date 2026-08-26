"""Tests for SuperGrok session vs API key detection."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from grok_build_cli_utilities.cli import app
from grok_build_cli_utilities.utils.auth_status import (
    AuthHistoryEvent,
    auth_history_change_points,
    detect_auth,
    format_auth_plan_advisor_line,
    format_weekly_reset_local,
    latest_prepaid_balance_usd,
    latest_weekly_usage_percent,
    load_auth_history,
    load_billing_snapshot,
    normalize_subscription_tier,
    period_bounds_from_cfg,
    subscription_tier_at,
    weekly_pool_reset_subject,
    weekly_usage_at,
)

runner = CliRunner()


def test_detect_session_wins_over_api_key(tmp_path: Path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text(
        json.dumps({"access_token": "x" * 40, "email": "u@example.com"}),
        encoding="utf-8",
    )
    env = {"XAI_API_KEY": "xai-test-key-12345678"}
    st = detect_auth(grok, env=env)
    assert st.effective == "supergrok_session"
    assert st.session_present
    assert st.api_key_env_present
    assert st.session_email == "u@example.com"
    assert any("wins" in n.lower() or "also set" in n.lower() for n in st.notes)


def test_detect_preferred_api_key_overrides_session(tmp_path: Path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text(
        json.dumps({"refresh_token": "y" * 40}),
        encoding="utf-8",
    )
    (grok / "config.toml").write_text(
        '[auth]\npreferred_method = "api_key"\n',
        encoding="utf-8",
    )
    st = detect_auth(grok, env={"XAI_API_KEY": "xai-abc"})
    assert st.effective == "api_key"
    assert st.preferred_method == "api_key"


def test_detect_api_key_only(tmp_path: Path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    st = detect_auth(grok, env={"XAI_API_KEY": "xai-only"})
    assert st.effective == "api_key"
    assert not st.session_present


def test_detect_none(tmp_path: Path, monkeypatch):
    grok = tmp_path / ".grok"
    grok.mkdir()
    # Clear ambient key if present in process env by passing empty env
    st = detect_auth(grok, env={})
    assert st.effective == "none"


def test_auth_history_change_points():
    evs = [
        AuthHistoryEvent("t1", "cached_token", "auth method selection"),
        AuthHistoryEvent("t2", "cached_token", "auth method selection"),
        AuthHistoryEvent("t3", "xai.api_key", "auth method selection"),
        AuthHistoryEvent("t4", "xai.api_key", "auth method selection"),
    ]
    ch = auth_history_change_points(evs)
    assert [e.method_id for e in ch] == ["cached_token", "xai.api_key"]


def test_latest_prepaid_and_weekly_from_billing_log(tmp_path: Path):
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    line = {
        "ts": "2026-08-07T12:00:00+00:00",
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 17.0,
                "prepaidBalance": {"val": 2140},
            }
        },
    }
    (log_dir / "unified.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    snap = load_billing_snapshot(grok)
    assert snap.prepaid_usd == 21.40
    assert snap.weekly_pct == 17.0
    assert snap.weekly_timeline and snap.weekly_timeline[-1][1] == 17.0
    assert snap.subscription_tier is None
    # wrappers still work (same one-pass implementation)
    assert latest_prepaid_balance_usd(grok) == 21.40
    assert latest_weekly_usage_percent(grok) == 17.0


def test_billing_snapshot_reads_subscription_tier(tmp_path: Path):
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    lines = [
        {
            "ts": "2026-08-16T12:00:00Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok",
                "config": {
                    "creditUsagePercent": 8.0,
                    "prepaidBalance": {"val": 15208},
                },
            },
        },
        {
            "ts": "2026-08-16T13:12:24Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok Heavy",
                "config": {
                    "creditUsagePercent": 10.0,
                    "prepaidBalance": {"val": 15208},
                },
            },
        },
    ]
    (log_dir / "unified.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )
    snap = load_billing_snapshot(grok)
    assert snap.subscription_tier == "heavy"
    assert snap.subscription_tier_raw == "SuperGrok Heavy"
    assert snap.weekly_pct == 10.0
    assert [t for _, t in snap.tier_timeline] == ["supergrok", "heavy"]
    from datetime import datetime, timezone

    assert (
        subscription_tier_at(datetime(2026, 8, 16, 12, 30, tzinfo=timezone.utc), snap.tier_timeline)
        == "supergrok"
    )
    assert (
        subscription_tier_at(datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc), snap.tier_timeline)
        == "heavy"
    )


def test_billing_weekly_timeline_keeps_earliest_plateau_ts(tmp_path: Path):
    """Same weekly % must not slide the first timestamp forward."""
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    lines = [
        {
            "ts": "2026-08-22T11:50:42Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok Heavy",
                "config": {
                    "creditUsagePercent": 15.0,
                    "prepaidBalance": {"val": 15208},
                },
            },
        },
        {
            "ts": "2026-08-22T12:15:07Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok Heavy",
                "config": {
                    "creditUsagePercent": 15.0,
                    "prepaidBalance": {"val": 15208},
                },
            },
        },
        {
            "ts": "2026-08-22T13:22:59Z",
            "msg": "billing: fetched credits config",
            "ctx": {
                "subscriptionTier": "SuperGrok Heavy",
                "config": {
                    "creditUsagePercent": 16.0,
                    "prepaidBalance": {"val": 15208},
                },
            },
        },
    ]
    (log_dir / "unified.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8"
    )
    snap = load_billing_snapshot(grok)
    assert len(snap.weekly_timeline) == 2
    first_dt, first_pct = snap.weekly_timeline[0]
    assert first_pct == 15.0
    assert first_dt.hour == 11 and first_dt.minute == 50
    assert snap.weekly_timeline[1][1] == 16.0
    from datetime import datetime, timezone

    mid = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    assert weekly_usage_at(mid, snap.weekly_timeline) == 15.0


def test_weekly_usage_holdback_in_pool_but_not_overage():
    from datetime import datetime, timezone

    pool = [(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), 15.0)]
    # Same week, before first sample, still in-pool → reuse 15%
    assert weekly_usage_at(datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc), pool) == 15.0
    # Older than one weekly period → still unknown
    assert weekly_usage_at(datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc), pool) is None
    # First remaining sample already overage → do not invent pool or 1.9
    over = [(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), 100.0)]
    assert weekly_usage_at(datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc), over) is None


def test_subscription_tier_holdback_only_when_unique():
    from datetime import datetime, timezone

    heavy_only = [(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), "heavy")]
    assert (
        subscription_tier_at(datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc), heavy_only)
        == "heavy"
    )
    mixed = [
        (datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc), "supergrok"),
        (datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), "heavy"),
    ]
    # Before the first sample of a mixed log: do not guess SuperGrok vs Heavy
    assert subscription_tier_at(datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc), mixed) is None


def test_normalize_subscription_tier():
    assert normalize_subscription_tier("SuperGrok Heavy") == "heavy"
    assert normalize_subscription_tier("SuperGrok") == "supergrok"
    assert normalize_subscription_tier(None) is None
    assert normalize_subscription_tier("nope") is None


def test_weekly_pool_reset_subject():
    assert weekly_pool_reset_subject("heavy") == "Weekly Heavy pool"
    assert weekly_pool_reset_subject("supergrok") == "Weekly SuperGrok pool"
    assert weekly_pool_reset_subject(None) == "Weekly included pool"


def test_plan_advisor_line_marks_heavy_current():
    from grok_build_cli_utilities.utils.auth_status import AuthStatus

    st = AuthStatus(
        effective="supergrok_session",
        label="SuperGrok session (login / cached_token)",
        session_present=True,
        session_path="/tmp/auth.json",
        session_email="u@example.com",
        api_key_env_present=False,
        preferred_method=None,
    )
    heavy = format_auth_plan_advisor_line(st, subscription_tier="heavy")
    assert heavy is not None
    assert "Heavy $300 is your current plan" in heavy
    assert "what-if" in heavy
    sg = format_auth_plan_advisor_line(st, subscription_tier="supergrok")
    assert sg is not None
    assert "Heavy rows are what-if" in sg


def test_billing_snapshot_reads_weekly_period(tmp_path: Path):
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    line = {
        "ts": "2026-08-24T19:35:43.534Z",
        "msg": "billing: fetched credits config",
        "ctx": {
            "subscriptionTier": "SuperGrok Heavy",
            "config": {
                "creditUsagePercent": 69.0,
                "prepaidBalance": {"val": 15208},
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-08-20T23:08:01.959078+00:00",
                    "end": "2026-08-27T23:08:01.959078+00:00",
                },
                "billingPeriodStart": "2026-08-20T23:08:01.959078+00:00",
                "billingPeriodEnd": "2026-08-27T23:08:01.959078+00:00",
            },
        },
    }
    (log_dir / "unified.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    snap = load_billing_snapshot(grok)
    assert snap.weekly_pct == 69.0
    assert snap.weekly_period_end is not None
    assert snap.weekly_period_end.year == 2026
    assert snap.weekly_period_end.month == 8
    assert snap.weekly_period_end.day == 27
    assert snap.weekly_period_end.hour == 23
    assert snap.weekly_period_end.minute == 8
    assert snap.weekly_period_start is not None
    assert snap.weekly_period_start.day == 20


def test_period_bounds_fallback_to_billing_period_end():
    start, end = period_bounds_from_cfg(
        {
            "billingPeriodStart": "2026-08-20T23:08:01+00:00",
            "billingPeriodEnd": "2026-08-27T23:08:01+00:00",
        }
    )
    assert start is not None and start.day == 20
    assert end is not None and end.day == 27 and end.hour == 23


def test_format_weekly_reset_local_new_york():
    import time

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        dt = datetime(2026, 8, 27, 23, 8, 1, tzinfo=timezone.utc)
        assert format_weekly_reset_local(dt) == "August 27, 19:08"
        assert format_weekly_reset_local(None) is None
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_auth_status_cli_shows_weekly_reset(tmp_path: Path):
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    (grok / "auth.json").write_text(
        json.dumps({"access_token": "x" * 40, "email": "u@example.com"}),
        encoding="utf-8",
    )
    line = {
        "ts": "2026-08-24T19:35:43.534Z",
        "msg": "billing: fetched credits config",
        "ctx": {
            "subscriptionTier": "SuperGrok Heavy",
            "config": {
                "creditUsagePercent": 69.0,
                "prepaidBalance": {"val": 15208},
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-08-20T23:08:01.959078+00:00",
                    "end": "2026-08-27T23:08:01.959078+00:00",
                },
            },
        },
    }
    (log_dir / "unified.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    r = runner.invoke(app, ["--grok-home", str(grok), "auth", "status", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["weekly_usage_pct"] == 69.0
    assert data["weekly_resets_at"] is not None
    assert data["weekly_resets_at"].startswith("2026-08-27T23:08:01")
    assert data["subscription_tier"] == "heavy"

    human = runner.invoke(app, ["--grok-home", str(grok), "auth", "status"])
    assert human.exit_code == 0, human.output
    reset = format_weekly_reset_local(datetime(2026, 8, 27, 23, 8, 1, 959078, tzinfo=timezone.utc))
    assert reset is not None
    assert "Weekly Heavy pool resets:" in human.output
    assert reset in human.output
    reset_lines = [
        ln for ln in human.output.splitlines() if "Weekly Heavy pool resets:" in ln and reset in ln
    ]
    assert len(reset_lines) == 1
    assert "Extra Credits" not in reset_lines[0]


def test_auth_status_cli_shows_extra_credits(tmp_path: Path):
    grok = tmp_path / ".grok"
    log_dir = grok / "logs"
    log_dir.mkdir(parents=True)
    (grok / "auth.json").write_text(
        json.dumps({"access_token": "x" * 40, "email": "u@example.com"}),
        encoding="utf-8",
    )
    line = {
        "ts": "2026-08-07T12:00:00+00:00",
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 6.5,
                "prepaidBalance": {"val": 2140},
            }
        },
    }
    (log_dir / "unified.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    r = runner.invoke(app, ["--grok-home", str(grok), "auth", "status", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["extra_credits_usd"] == 21.4
    assert data["weekly_usage_pct"] == 6.5
    assert data["auth"]["effective"] == "supergrok_session"
    assert data.get("subscription_tier") is None


def test_load_auth_history_from_log(tmp_path: Path):
    grok = tmp_path / ".grok"
    logdir = grok / "logs"
    logdir.mkdir(parents=True)
    lines = [
        {
            "ts": "2026-08-06T12:00:00Z",
            "msg": "auth method selection",
            "ctx": {"default_auth_method_id": "cached_token"},
        },
        {
            "ts": "2026-08-07T12:00:00Z",
            "msg": "pager eager auth method selected",
            "ctx": {"method_id": "xai.api_key"},
        },
    ]
    (logdir / "unified.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )
    evs = load_auth_history(grok)
    assert len(evs) == 2
    assert evs[0].method_id == "cached_token"
    assert evs[1].method_id == "xai.api_key"


def test_auth_status_cli_json(tmp_path: Path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text(
        json.dumps({"access_token": "z" * 40}),
        encoding="utf-8",
    )
    r = runner.invoke(
        app,
        ["-g", str(grok), "auth", "status", "--json"],
        env={**os.environ, "XAI_API_KEY": ""},
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout or r.output)
    assert data["auth"]["effective"] == "supergrok_session"
