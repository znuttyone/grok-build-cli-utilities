"""Detect Grok Build auth path: SuperGrok session vs API key.

Mirrors Build priority (cached_token / auth.json session wins over XAI_API_KEY
unless config [auth] preferred_method = \"api_key\").

Current machine state is reliable. Historical path changes can be inferred
(optionally) from ~/.grok/logs/unified.jsonl auth method selection events —
not per-turn billing labels.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import load_toml

# Build log msg values that carry default_auth_method_id / method_id
_AUTH_METHOD_MSGS = frozenset(
    {
        "auth method selection",
        "pager eager auth method selected",
        "auth: initialize() built auth_methods for ACP response",
    }
)


@dataclass
class AuthStatus:
    """Current effective auth path for this grok home / env."""

    effective: str  # supergrok_session | api_key | none
    label: str  # human, /session-info style
    session_present: bool
    session_path: str
    session_email: str | None
    api_key_env_present: bool
    preferred_method: str | None
    notes: list[str] = field(default_factory=list)
    spend_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuthHistoryEvent:
    ts: str
    method_id: str  # cached_token | xai.api_key | other
    source_msg: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _walk_tokenish(obj: Any) -> bool:
    """True if nested JSON looks like a live session credential store."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if (
                kl
                in (
                    "access_token",
                    "refresh_token",
                    "id_token",
                    "token",
                    "session_token",
                )
                and isinstance(v, str)
                and len(v.strip()) > 8
            ):
                return True
            # OIDC-style nested "key" that is not empty
            if kl in ("key", "api_key") and isinstance(v, str) and len(v.strip()) > 8:
                # avoid treating tiny placeholders
                if not v.startswith("xai-") and "token" not in kl:
                    # still count long secrets
                    if len(v) > 16:
                        return True
                else:
                    return True
            if _walk_tokenish(v):
                return True
    elif isinstance(obj, list):
        for item in obj[:50]:
            if _walk_tokenish(item):
                return True
    return False


def _find_email(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if (
                str(k).lower() in ("email", "user_email", "preferred_username")
                and isinstance(v, str)
                and "@" in v
            ):
                return v
            found = _find_email(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj[:30]:
            found = _find_email(item)
            if found:
                return found
    return None


def _load_preferred_method(grok_home: Path) -> str | None:
    for name in ("config.toml", "grok-utils.toml"):
        data = load_toml(grok_home / name)
        if not data:
            continue
        auth = data.get("auth")
        if isinstance(auth, dict):
            pref = auth.get("preferred_method")
            if isinstance(pref, str) and pref.strip():
                return pref.strip().lower()
    return None


def detect_auth(grok_home: Path | str, *, env: dict[str, str] | None = None) -> AuthStatus:
    """Detect effective Build auth path (current machine)."""
    home = Path(grok_home)
    environ = env if env is not None else os.environ
    auth_path = home / "auth.json"
    session_present = False
    email: str | None = None
    notes: list[str] = []

    if auth_path.is_file():
        try:
            raw = auth_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            session_present = _walk_tokenish(data)
            email = _find_email(data)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            notes.append("auth.json present but unreadable/invalid JSON")

    api_key = (environ.get("XAI_API_KEY") or "").strip()
    api_present = bool(api_key)
    preferred = _load_preferred_method(home)

    # preferred_method=api_key forces API when a key exists
    if preferred in ("api_key", "xai.api_key", "api-key", "apikey"):
        if api_present:
            effective = "api_key"
            label = "API key (XAI_API_KEY)"
            notes.append("config [auth] preferred_method prefers API key")
            if session_present:
                notes.append("SuperGrok session file also present; preferred_method overrides")
        elif session_present:
            effective = "supergrok_session"
            label = "SuperGrok session (login / cached_token)"
            notes.append("preferred_method=api_key but XAI_API_KEY unset; session still usable")
        else:
            effective = "none"
            label = "No auth detected"
            notes.append("preferred_method=api_key but no API key and no session")
    elif session_present:
        effective = "supergrok_session"
        label = "SuperGrok session (login / cached_token)"
        if api_present:
            notes.append(
                "XAI_API_KEY also set; session token wins unless "
                '[auth] preferred_method = "api_key"'
            )
    elif api_present:
        effective = "api_key"
        label = "API key (XAI_API_KEY)"
    else:
        effective = "none"
        label = "No auth detected"
        notes.append("No auth.json session and XAI_API_KEY unset")

    if effective == "supergrok_session":
        spend = "Weekly pool + SuperGrok auto top-ups; often missing from X console API-key Usage."
    elif effective == "api_key":
        spend = "API prepaid / paygo lens; console Usage may still lag."
    else:
        spend = "Cannot infer spend path without session or API key."

    return AuthStatus(
        effective=effective,
        label=label,
        session_present=session_present,
        session_path=str(auth_path),
        session_email=email,
        api_key_env_present=api_present,
        preferred_method=preferred,
        notes=notes,
        spend_hint=spend,
    )


def format_auth_short(status: AuthStatus) -> list[str]:
    """Lines for usage cost/report footer (no secrets)."""
    lines = [
        f"Auth path (this machine · now): {status.label}",
        f"  Spend lens: {status.spend_hint}",
    ]
    for n in status.notes[:3]:
        lines.append(f"  Note: {n}")
    lines.append(
        "  Auth is current machine state — not per historical turn. "
        "Timeline: grok-utils auth status --history"
    )
    return lines


def format_auth_plan_advisor_line(
    status: AuthStatus,
    *,
    subscription_tier: str | None = None,
) -> str | None:
    """Plan-advisor footer: which table rows are the current bill vs what-if."""
    if status.effective == "api_key":
        return (
            "Auth now: API key — SuperGrok/Heavy rows are what-if "
            "(not your current bill unless you grok login)"
        )
    if status.effective != "supergrok_session":
        return None
    if subscription_tier == "heavy":
        return (
            "Auth now: Heavy session — SuperGrok $30 and Pure API rows are "
            "what-if (not your current bill). Heavy $300 is your current plan."
        )
    if subscription_tier == "supergrok":
        return (
            "Auth now: SuperGrok session — Pure API and Heavy rows are "
            "what-if (not your current bill)."
        )
    return "Auth now: SuperGrok session — Pure API row is a what-if (not your current bill)"


def _method_from_ctx(ctx: dict[str, Any]) -> str | None:
    for key in ("default_auth_method_id", "method_id", "selected_method_id"):
        v = ctx.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _normalize_method(mid: str) -> str:
    m = mid.lower()
    if m in ("cached_token", "session_token", "oidc", "grok.com"):
        if m == "grok.com":
            return "grok.com"
        return "cached_token"
    if m in ("xai.api_key", "api_key", "api-key"):
        return "xai.api_key"
    return mid


def iter_auth_history_events(
    grok_home: Path | str,
    *,
    max_events: int = 500,
    max_bytes: int = 8_000_000,
) -> Iterator[AuthHistoryEvent]:
    """Yield auth method selection events from unified.jsonl (oldest first among kept)."""
    log_path = Path(grok_home) / "logs" / "unified.jsonl"
    if not log_path.is_file():
        return

    # Read tail if huge
    try:
        size = log_path.stat().st_size
    except OSError:
        return

    events: list[AuthHistoryEvent] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(max(0, size - max_bytes))
                f.readline()  # drop partial line
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("msg") or ""
                if msg not in _AUTH_METHOD_MSGS and "auth method" not in str(msg).lower():
                    continue
                ctx = obj.get("ctx")
                if not isinstance(ctx, dict):
                    continue
                mid = _method_from_ctx(ctx)
                if not mid:
                    continue
                ts = str(obj.get("ts") or "")
                events.append(
                    AuthHistoryEvent(
                        ts=ts,
                        method_id=_normalize_method(mid),
                        source_msg=str(msg),
                    )
                )
    except OSError:
        return

    # keep last max_events
    if len(events) > max_events:
        events = events[-max_events:]
    yield from events


def auth_history_change_points(events: list[AuthHistoryEvent]) -> list[AuthHistoryEvent]:
    """Collapse consecutive same method_id; keep first event of each run."""
    if not events:
        return []
    out: list[AuthHistoryEvent] = [events[0]]
    for ev in events[1:]:
        if ev.method_id != out[-1].method_id:
            out.append(ev)
    return out


def load_auth_history(grok_home: Path | str) -> list[AuthHistoryEvent]:
    return list(iter_auth_history_events(grok_home))


def method_id_to_auth_effective(method_id: str) -> str:
    """Map log method_id → detect_auth effective string."""
    m = (method_id or "").lower()
    if m in ("xai.api_key", "api_key", "api-key"):
        return "api_key"
    if m in ("cached_token", "session_token", "oidc", "grok.com"):
        return "supergrok_session"
    return "unknown"


def auth_effective_at(
    ts: datetime,
    change_points: list[AuthHistoryEvent],
    *,
    fallback: str = "unknown",
) -> str:
    """Auth path at timestamp using change-points (method holds until next change)."""
    if not change_points:
        return fallback
    # Parse event times; pick last change_point with event_ts <= ts
    chosen = fallback
    for ev in change_points:
        try:
            raw = ev.ts.replace("Z", "+00:00") if ev.ts.endswith("Z") else ev.ts
            ev_dt = datetime.fromisoformat(raw)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if ev_dt <= t:
            chosen = method_id_to_auth_effective(ev.method_id)
        else:
            break
    return chosen


# Session turns often outlive unified.jsonl (Build truncates the log).
# creditUsagePercent is cumulative within a weekly period: if the first
# remaining sample is still in-pool, earlier same-week turns were too.
BILLING_HOLD_BACK = timedelta(days=7)
# Keep in lockstep with pricing.OVERAGE_WEEKLY_PCT_THRESHOLD (no import cycle).
BILLING_POOL_HOLD_BACK_BELOW = 99.0


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def parse_iso_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from a billing log field."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_or_none(dt: datetime | None) -> str | None:
    """JSON-friendly ISO-8601, or None."""
    if dt is None:
        return None
    return dt.isoformat()


_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def format_weekly_reset_local(dt: datetime | None) -> str | None:
    """Build /usage-style reset clock: 'August 27, 19:08' in the local timezone."""
    if dt is None:
        return None
    local = dt.astimezone()
    return f"{_MONTHS_EN[local.month - 1]} {local.day}, {local.strftime('%H:%M')}"


def period_bounds_from_cfg(cfg: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """Weekly pool window from a billing config object.

    Prefer ``currentPeriod.{start,end}`` (what /usage "Resets" uses); fall back
    to ``billingPeriodStart`` / ``billingPeriodEnd`` (same values on current
    Build logs).
    """
    start: datetime | None = None
    end: datetime | None = None
    period = cfg.get("currentPeriod")
    if isinstance(period, dict):
        start = parse_iso_dt(period.get("start"))
        end = parse_iso_dt(period.get("end"))
    if start is None:
        start = parse_iso_dt(cfg.get("billingPeriodStart"))
    if end is None:
        end = parse_iso_dt(cfg.get("billingPeriodEnd"))
    return start, end


def _within_hold_back(ts: datetime, first_dt: datetime) -> bool:
    t = _as_utc(ts)
    f = _as_utc(first_dt)
    return t < f and (f - t) <= BILLING_HOLD_BACK


def weekly_usage_at(
    ts: datetime,
    timeline: list[tuple[datetime, float]],
) -> float | None:
    """Last known weekly usage % at or before ts; None if before first sample.

    When ``ts`` is before the first sample but within ``BILLING_HOLD_BACK``
    and that first reading is still below overage, return it (inferred
    same-week pool). Do not invent overage if the first sample is ~100%.
    """
    if not timeline:
        return None
    t = _as_utc(ts)
    chosen: float | None = None
    for dt, pct in timeline:
        if _as_utc(dt) <= t:
            chosen = pct
        else:
            break
    if chosen is not None:
        return chosen
    first_dt, first_pct = timeline[0]
    if first_pct < BILLING_POOL_HOLD_BACK_BELOW and _within_hold_back(t, first_dt):
        return first_pct
    return None


def normalize_subscription_tier(raw: str | None) -> str | None:
    """Map billing ``subscriptionTier`` → ``heavy`` | ``supergrok`` | None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    if "heavy" in s:
        return "heavy"
    if "supergrok" in s or s in ("super", "sg"):
        return "supergrok"
    return None


def subscription_tier_label(tier: str | None) -> str:
    """Human plan name for footers (Heavy / SuperGrok / subscription)."""
    if tier == "heavy":
        return "Heavy"
    if tier == "supergrok":
        return "SuperGrok"
    return "subscription"


def weekly_pool_reset_subject(tier: str | None) -> str:
    """What ``currentPeriod.end`` resets: the included weekly pool, not Extra Credits."""
    lab = subscription_tier_label(tier)
    if lab == "subscription":
        return "Weekly included pool"
    return f"Weekly {lab} pool"


def subscription_tier_at(
    ts: datetime,
    timeline: list[tuple[datetime, str]],
) -> str | None:
    """Last known normalized tier at or before ts; None if before first sample.

    Before the first sample (within ``BILLING_HOLD_BACK``), reuse that sample's
    tier only when the log never shows another plan (so an upgrade is not
    projected backward).
    """
    if not timeline:
        return None
    t = _as_utc(ts)
    chosen: str | None = None
    for dt, tier in timeline:
        if _as_utc(dt) <= t:
            chosen = tier
        else:
            break
    if chosen is not None:
        return chosen
    first_dt, first_tier = timeline[0]
    unique = {tier for _, tier in timeline}
    if len(unique) == 1 and _within_hold_back(t, first_dt):
        return first_tier
    return None


def _empty_billing(
    *,
    prepaid_usd: float | None = None,
    weekly_period_start: datetime | None = None,
    weekly_period_end: datetime | None = None,
) -> BillingSnapshot:
    return BillingSnapshot(
        weekly_timeline=[],
        prepaid_usd=prepaid_usd,
        weekly_pct=None,
        subscription_tier=None,
        subscription_tier_raw=None,
        tier_timeline=[],
        weekly_period_start=weekly_period_start,
        weekly_period_end=weekly_period_end,
    )


@dataclass
class BillingSnapshot:
    """Latest SuperGrok/Heavy billing sample from unified.jsonl (one log pass)."""

    weekly_timeline: list[tuple[datetime, float]]
    prepaid_usd: float | None
    weekly_pct: float | None
    subscription_tier: str | None = None  # heavy | supergrok
    subscription_tier_raw: str | None = None
    tier_timeline: list[tuple[datetime, str]] = field(default_factory=list)
    # currentPeriod / billingPeriod* — /usage "Resets" is weekly_period_end local
    weekly_period_start: datetime | None = None
    weekly_period_end: datetime | None = None


# Cost windows need the upgrade change-point; 8MB covers typical unified.jsonl.
DEFAULT_BILLING_SCAN_BYTES = 8_000_000


def load_billing_snapshot(
    grok_home: Path | str,
    *,
    max_bytes: int = DEFAULT_BILLING_SCAN_BYTES,
) -> BillingSnapshot:
    """Parse billing: weekly %, prepaid $, plan, and weekly pool window."""
    log_path = Path(grok_home) / "logs" / "unified.jsonl"
    if not log_path.is_file():
        return _empty_billing()
    try:
        size = log_path.stat().st_size
    except OSError:
        return _empty_billing()

    raw: list[tuple[datetime, float]] = []
    raw_tiers: list[tuple[datetime, str, str]] = []
    last_prepaid: float | None = None
    last_tier_raw: str | None = None
    last_period_start: datetime | None = None
    last_period_end: datetime | None = None
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(max(0, size - max_bytes))
                f.readline()
            for line in f:
                if "billing: fetched credits config" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ctx = obj.get("ctx") or {}
                if not isinstance(ctx, dict):
                    ctx = {}
                cfg = ctx.get("config") or {}
                if not isinstance(cfg, dict):
                    cfg = {}
                # prepaid (cents → USD)
                pb = cfg.get("prepaidBalance") or {}
                if isinstance(pb, dict) and pb.get("val") is not None:
                    try:
                        last_prepaid = float(pb["val"]) / 100.0
                    except (TypeError, ValueError):
                        pass
                period_start, period_end = period_bounds_from_cfg(cfg)
                if period_start is not None:
                    last_period_start = period_start
                if period_end is not None:
                    last_period_end = period_end
                ts_s = str(obj.get("ts") or "")
                raw_ts = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
                try:
                    dt = datetime.fromisoformat(raw_ts) if raw_ts else None
                    if dt is not None and dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    dt = None
                # Plan sits next to config (not inside it): ctx.subscriptionTier
                raw_tier = ctx.get("subscriptionTier") or cfg.get("subscriptionTier")
                if isinstance(raw_tier, str) and raw_tier.strip() and dt is not None:
                    norm = normalize_subscription_tier(raw_tier)
                    if norm:
                        last_tier_raw = raw_tier.strip()
                        raw_tiers.append((dt, norm, raw_tier.strip()))
                # weekly %
                cu = cfg.get("creditUsagePercent")
                if cu is None or dt is None:
                    continue
                try:
                    pct = float(cu)
                except (TypeError, ValueError):
                    continue
                raw.append((dt, pct))
    except OSError:
        return _empty_billing(
            prepaid_usd=last_prepaid,
            weekly_period_start=last_period_start,
            weekly_period_end=last_period_end,
        )

    raw.sort(key=lambda x: x[0])
    out: list[tuple[datetime, float]] = []
    if raw:
        out = [raw[0]]
        for dt, pct in raw[1:]:
            if abs(pct - out[-1][1]) >= 0.5:
                out.append((dt, pct))
            else:
                # Keep earliest ts of this plateau so weekly_usage_at covers
                # it; only the % updates. Moving ts forward opened a growing
                # "unknown" gap and billed historical pool usage at list$.
                out[-1] = (out[-1][0], pct)

    raw_tiers.sort(key=lambda x: x[0])
    tier_out: list[tuple[datetime, str]] = []
    for dt, norm, _raw in raw_tiers:
        if not tier_out or tier_out[-1][1] != norm:
            tier_out.append((dt, norm))

    last_norm = tier_out[-1][1] if tier_out else None
    return BillingSnapshot(
        weekly_timeline=out,
        prepaid_usd=last_prepaid,
        weekly_pct=out[-1][1] if out else None,
        subscription_tier=last_norm,
        subscription_tier_raw=last_tier_raw,
        tier_timeline=tier_out,
        weekly_period_start=last_period_start,
        weekly_period_end=last_period_end,
    )


def load_weekly_usage_timeline(
    grok_home: Path | str,
    *,
    max_bytes: int = 8_000_000,
) -> list[tuple[datetime, float]]:
    """Timeline of (utc_ts, creditUsagePercent) from billing log, oldest first."""
    return load_billing_snapshot(grok_home, max_bytes=max_bytes).weekly_timeline


def latest_weekly_usage_percent(
    grok_home: Path | str,
    *,
    max_bytes: int = 2_000_000,
) -> float | None:
    """Latest creditUsagePercent from billing log."""
    return load_billing_snapshot(grok_home, max_bytes=max_bytes).weekly_pct


def latest_prepaid_balance_usd(
    grok_home: Path | str,
    *,
    max_bytes: int = 2_000_000,
) -> float | None:
    """Latest prepaidBalance.val from billing log (cents → USD)."""
    return load_billing_snapshot(grok_home, max_bytes=max_bytes).prepaid_usd


def latest_subscription_tier(
    grok_home: Path | str,
    *,
    max_bytes: int = DEFAULT_BILLING_SCAN_BYTES,
) -> str | None:
    """Latest normalized subscription tier (heavy | supergrok) from billing log."""
    return load_billing_snapshot(grok_home, max_bytes=max_bytes).subscription_tier
