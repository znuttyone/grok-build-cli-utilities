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
from datetime import datetime, timezone
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
            if kl in (
                "access_token",
                "refresh_token",
                "id_token",
                "token",
                "session_token",
            ) and isinstance(v, str) and len(v.strip()) > 8:
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
            notes.append('config [auth] preferred_method prefers API key')
            if session_present:
                notes.append("SuperGrok session file also present; preferred_method overrides")
        elif session_present:
            effective = "supergrok_session"
            label = "SuperGrok session (login / cached_token)"
            notes.append(
                "preferred_method=api_key but XAI_API_KEY unset; session still usable"
            )
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
        spend = (
            "Weekly pool + SuperGrok auto top-ups; often missing from X console API-key Usage."
        )
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


def format_auth_plan_advisor_line(status: AuthStatus) -> str | None:
    if status.effective == "supergrok_session":
        return (
            "Auth now: SuperGrok session — Pure API row is a what-if "
            "(not your current bill)"
        )
    if status.effective == "api_key":
        return (
            "Auth now: API key — SuperGrok/Heavy rows are what-if "
            "(not your current bill unless you grok login)"
        )
    return None


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


def weekly_usage_at(
    ts: datetime,
    timeline: list[tuple[datetime, float]],
) -> float | None:
    """Last known weekly usage % at or before ts; None if before first sample."""
    if not timeline:
        return None
    t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    chosen: float | None = None
    for dt, pct in timeline:
        if dt <= t:
            chosen = pct
        else:
            break
    return chosen


@dataclass
class BillingSnapshot:
    """Latest SuperGrok billing sample from unified.jsonl (one log pass)."""

    weekly_timeline: list[tuple[datetime, float]]
    prepaid_usd: float | None
    weekly_pct: float | None


def load_billing_snapshot(
    grok_home: Path | str,
    *,
    max_bytes: int = 2_000_000,
) -> BillingSnapshot:
    """Parse billing: fetched credits config once → weekly timeline + prepaid $."""
    log_path = Path(grok_home) / "logs" / "unified.jsonl"
    if not log_path.is_file():
        return BillingSnapshot(weekly_timeline=[], prepaid_usd=None, weekly_pct=None)
    try:
        size = log_path.stat().st_size
    except OSError:
        return BillingSnapshot(weekly_timeline=[], prepaid_usd=None, weekly_pct=None)

    raw: list[tuple[datetime, float]] = []
    last_prepaid: float | None = None
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
                cfg = (obj.get("ctx") or {}).get("config") or {}
                # prepaid (cents → USD)
                pb = cfg.get("prepaidBalance") or {}
                if isinstance(pb, dict) and pb.get("val") is not None:
                    try:
                        last_prepaid = float(pb["val"]) / 100.0
                    except (TypeError, ValueError):
                        pass
                # weekly %
                cu = cfg.get("creditUsagePercent")
                ts_s = str(obj.get("ts") or "")
                if cu is None or not ts_s:
                    continue
                try:
                    pct = float(cu)
                    raw_ts = ts_s.replace("Z", "+00:00") if ts_s.endswith("Z") else ts_s
                    dt = datetime.fromisoformat(raw_ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                raw.append((dt, pct))
    except OSError:
        return BillingSnapshot(weekly_timeline=[], prepaid_usd=last_prepaid, weekly_pct=None)

    if not raw:
        return BillingSnapshot(
            weekly_timeline=[], prepaid_usd=last_prepaid, weekly_pct=None
        )
    raw.sort(key=lambda x: x[0])
    out: list[tuple[datetime, float]] = [raw[0]]
    for dt, pct in raw[1:]:
        if abs(pct - out[-1][1]) >= 0.5:
            out.append((dt, pct))
        else:
            out[-1] = (dt, pct)
    return BillingSnapshot(
        weekly_timeline=out,
        prepaid_usd=last_prepaid,
        weekly_pct=out[-1][1] if out else None,
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
