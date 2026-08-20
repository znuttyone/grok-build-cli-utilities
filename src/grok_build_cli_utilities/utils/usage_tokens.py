"""Turn-level usage from session updates.jsonl (token-accurate cost foundation)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from rich.progress import Progress

from .pricing import TokenRates, api_estimate_usd, rates_for_model

# xAI / Build: 1 USD = 10^10 costUsdTicks. /usage Session Cost uses this.
# https://docs.x.ai/developers/cost-tracking
TICKS_PER_USD = 10_000_000_000
# Prompt ≥ this many tokens bills the whole request at 2× list rates.
LONG_CONTEXT_PROMPT_TOKENS = 200_000


@dataclass
class UsageRec:
    """One deduped turn_completed usage record."""

    prompt_id: str
    ts: datetime
    project: str  # short app name
    cwd: str  # decoded path when available
    session_id: str
    model: str = "unknown"
    input: int = 0
    output: int = 0
    total: int = 0
    cached: int = 0
    reasoning: int = 0
    ticks: int = 0
    model_calls: int = 0


@dataclass
class UsageBucket:
    """Aggregate usage for a group key."""

    key: str
    n: int = 0
    input: int = 0
    output: int = 0
    total: int = 0
    cached: int = 0
    reasoning: int = 0
    ticks: int = 0
    model_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)

    def add(self, r: UsageRec) -> None:
        self.n += 1
        self.input += r.input
        self.output += r.output
        self.total += r.total
        self.cached += r.cached
        self.reasoning += r.reasoning
        self.ticks += r.ticks
        self.model_calls += r.model_calls
        if r.model:
            self.models[r.model] = self.models.get(r.model, 0) + 1

    @property
    def uncached_in(self) -> int:
        return max(self.input - self.cached, 0)

    @property
    def cache_pct(self) -> float:
        return (100.0 * self.cached / self.total) if self.total else 0.0

    def primary_model(self) -> str:
        if not self.models:
            return "unknown"
        return max(self.models.items(), key=lambda kv: kv[1])[0]

    def api_est(self, rates: TokenRates | None = None) -> float:
        """Rate-table estimate (no ticks). Prefer ``list_usd`` for /usage-matching $."""
        r = rates or rates_for_model(self.primary_model())
        out_n = completion_tokens(
            output=self.output,
            reasoning=self.reasoning,
            total=self.total,
            input_tokens=self.input,
        )
        return api_estimate_usd(
            cached=self.cached,
            uncached_in=self.uncached_in,
            output=out_n,
            reasoning=0,
            rates=r,
            reason_as_output=False,
        )

    def list_usd(self, rates: TokenRates | None = None, *, prefer_ticks: bool = True) -> float:
        """list$ for this bucket: costUsdTicks/1e10 when present, else rate table."""
        if prefer_ticks and self.ticks > 0:
            return list_price_usd(self.ticks)
        return self.api_est(rates)

    def to_dict(self, rates: TokenRates | None = None) -> dict:
        return {
            "key": self.key,
            "prompts": self.n,
            "input": self.input,
            "cached": self.cached,
            "uncached_in": self.uncached_in,
            "output": self.output,
            "reasoning": self.reasoning,
            "total": self.total,
            "cache_pct": round(self.cache_pct, 2),
            "ticks": self.ticks,
            "model_calls": self.model_calls,
            "primary_model": self.primary_model(),
            "api_est_usd": round(self.api_est(rates), 4),
        }


def parse_ts(obj: dict) -> datetime | None:
    meta = (obj.get("params") or {}).get("_meta") or {}
    ms = meta.get("agentTimestampMs")
    if ms is not None:
        try:
            return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    ts = obj.get("timestamp")
    if isinstance(ts, (int, float)):
        try:
            if ts > 1e12:
                return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def project_from_path(updates_path: Path) -> tuple[str, str]:
    """Return (short_app_name, decoded_cwd_or_parent)."""
    parent = updates_path.parent.parent
    name = parent.name
    decoded = name.replace("%2F", "/")
    cwd = decoded if decoded.startswith("/") else str(parent)
    if "GitHub/" in decoded:
        short = decoded.split("GitHub/")[-1].rstrip("/")
    else:
        short = Path(decoded).name or name
    return short, cwd


def _primary_model_from_usage(usage: dict) -> str:
    mu = usage.get("modelUsage")
    if isinstance(mu, dict) and mu:
        # Prefer model with highest totalTokens
        best = None
        best_tot = -1
        for mid, stats in mu.items():
            if not isinstance(stats, dict):
                continue
            tot = int(stats.get("totalTokens") or 0)
            if tot >= best_tot:
                best_tot = tot
                best = str(mid)
        if best:
            return best
    return "unknown"


def iter_turn_usage_files(sessions_dir: Path) -> Iterator[Path]:
    if not sessions_dir.is_dir():
        return
    yield from sessions_dir.rglob("updates.jsonl")


def load_turn_usage(
    sessions_dir: Path,
    *,
    progress: Progress | None = None,
) -> list[UsageRec]:
    """Load and dedupe turn_completed usage events from all sessions."""
    paths = list(iter_turn_usage_files(sessions_dir))
    task = None
    if progress and paths:
        task = progress.add_task("Scanning turn usage...", total=len(paths))

    by_prompt: dict[str, UsageRec] = {}
    for path in paths:
        project, cwd = project_from_path(path)
        session_fallback = path.parent.name
        try:
            f = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            if progress and task is not None:
                progress.advance(task)
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("method") not in ("session/update", "_x.ai/session/update"):
                    continue
                params = obj.get("params") or {}
                update = params.get("update") or {}
                if update.get("sessionUpdate") != "turn_completed":
                    continue
                usage = update.get("usage")
                if not isinstance(usage, dict) or "inputTokens" not in usage:
                    continue
                ts = parse_ts(obj)
                if ts is None:
                    continue
                session_id = str(params.get("sessionId") or session_fallback)
                prompt_id = str(
                    update.get("prompt_id")
                    or update.get("promptId")
                    or f"{session_id}:{obj.get('timestamp')}"
                )
                rec = UsageRec(
                    prompt_id=prompt_id,
                    ts=ts,
                    project=project,
                    cwd=cwd,
                    session_id=session_id,
                    model=_primary_model_from_usage(usage),
                    input=int(usage.get("inputTokens") or 0),
                    output=int(usage.get("outputTokens") or 0),
                    total=int(usage.get("totalTokens") or 0),
                    cached=int(usage.get("cachedReadTokens") or 0),
                    reasoning=int(usage.get("reasoningTokens") or 0),
                    ticks=int(usage.get("costUsdTicks") or 0),
                    model_calls=int(usage.get("modelCalls") or 0),
                )
                prev = by_prompt.get(rec.prompt_id)
                if prev is None or rec.total >= prev.total:
                    by_prompt[rec.prompt_id] = rec
        if progress and task is not None:
            progress.advance(task)

    return sorted(by_prompt.values(), key=lambda r: r.ts)


def filter_usage(
    records: list[UsageRec],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    apps: list[str] | None = None,
) -> list[UsageRec]:
    out: list[UsageRec] = []
    needles = [a.lower() for a in apps] if apps else None
    for r in records:
        d = r.ts.date()
        if date_from is not None and d < date_from:
            continue
        if date_to is not None and d > date_to:
            continue
        if needles is not None:
            pl = r.project.lower()
            cl = r.cwd.lower()
            if not any(n in pl or n in cl or pl in n for n in needles):
                continue
        out.append(r)
    return out


def bucket_key(r: UsageRec, group: str) -> str:
    if group in ("app", "project"):
        # app = short folder name; project = full cwd (disambiguates same name in different paths)
        return r.project if group == "app" else (r.cwd or r.project)
    if group == "model":
        return r.model or "unknown"
    if group == "day":
        return r.ts.date().isoformat()
    if group == "week":
        iso = r.ts.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if group == "month":
        return f"{r.ts.year}-{r.ts.month:02d}"
    if group == "none":
        return "all"
    raise ValueError(f"Unknown group: {group}")


def aggregate(records: list[UsageRec], group: str) -> list[UsageBucket]:
    m: dict[str, UsageBucket] = {}
    for r in records:
        k = bucket_key(r, group)
        if k not in m:
            m[k] = UsageBucket(key=k)
        m[k].add(r)
    return [m[k] for k in sorted(m.keys())]


def total_bucket(records: list[UsageRec]) -> UsageBucket:
    t = UsageBucket(key="TOTAL")
    for r in records:
        t.add(r)
    return t


def allocate_invoice(
    buckets: list[UsageBucket],
    *,
    invoice_usd: float,
    fixed_usd: float = 0.0,
    group: str = "app",
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[float, float]:
    """Return (scale_per_tick, fixed_per_bucket).

    var$ = ticks * scale; tot$ = var$ + fixed_per_bucket.
    """
    total_ticks = sum(b.ticks for b in buckets)
    if total_ticks <= 0:
        return 0.0, 0.0
    scale = float(invoice_usd) / total_ticks
    if group == "day" and date_from and date_to:
        n_cal = (date_to - date_from).days + 1
        fixed_per = float(fixed_usd) / n_cal if n_cal else 0.0
    else:
        fixed_per = float(fixed_usd) / len(buckets) if buckets else 0.0
    return scale, fixed_per


@dataclass
class PaygoTypeUsd:
    """Console Text-type Spend breakdown (from Usage explorer Type legend)."""

    cached: float = 0.0  # Cached prompt text tokens $
    input: float = 0.0  # Uncached prompt text tokens $
    output: float = 0.0  # Completion text tokens $
    reasoning: float = 0.0  # Reasoning text tokens $

    @property
    def total(self) -> float:
        return float(self.cached) + float(self.input) + float(self.output) + float(self.reasoning)

    def as_dict(self) -> dict[str, float]:
        return {
            "cached_usd": self.cached,
            "input_usd": self.input,
            "output_usd": self.output,
            "reasoning_usd": self.reasoning,
            "total_usd": self.total,
        }


def allocate_paygo(
    buckets: list[UsageBucket],
    *,
    paygo_usd: float,
    weight: str = "ticks",
    rates: TokenRates | None = None,
) -> dict[str, float]:
    """Allocate a known console/paygo cash total across buckets (single pool).

    weight:
      - ticks: costUsdTicks (default; matches internal metering intensity)
      - api: pure-API modeled $ share (uses rates if provided)
      - tokens: total token share

    Returns map key -> allocated USD. Sum equals paygo_usd (within float noise).
    Prefer allocate_paygo_by_type when console Type $ breakdown is available.
    """
    if paygo_usd < 0 or not buckets:
        return {}

    weights: dict[str, float] = {}
    for b in buckets:
        if weight == "api":
            w = float(b.api_est(rates))
        elif weight == "tokens":
            w = float(b.total)
        else:  # ticks
            w = float(b.ticks) if b.ticks > 0 else float(b.total)
        weights[b.key] = max(w, 0.0)

    total_w = sum(weights.values())
    if total_w <= 0:
        # equal split fallback
        share = float(paygo_usd) / len(buckets)
        return {b.key: share for b in buckets}

    return {k: float(paygo_usd) * (w / total_w) for k, w in weights.items()}


def allocate_paygo_by_type(
    buckets: list[UsageBucket],
    types: PaygoTypeUsd,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Allocate console Type-level $ by each bucket's share of that token type.

    Example (console Text legend for window):
      cached $3.28, prompt $1.76, completion $0.25, reasoning $0.05

    bill$[app] = sum over types of (app.tokens_type / total.tokens_type) * console_usd_type

    Returns (bill_by_key, detail_by_key) where detail has cached/input/output/reasoning $.
    Sum of bill_by_key equals types.total (within float noise).
    """
    if not buckets or types.total <= 0:
        return {}, {}

    sum_cached = sum(b.cached for b in buckets)
    sum_in = sum(b.uncached_in for b in buckets)
    sum_out = sum(b.output for b in buckets)
    sum_reason = sum(b.reasoning for b in buckets)

    bill: dict[str, float] = {}
    detail: dict[str, dict[str, float]] = {}

    for b in buckets:
        c = (b.cached / sum_cached * types.cached) if sum_cached > 0 and types.cached else 0.0
        i = (b.uncached_in / sum_in * types.input) if sum_in > 0 and types.input else 0.0
        o = (b.output / sum_out * types.output) if sum_out > 0 and types.output else 0.0
        r = (
            (b.reasoning / sum_reason * types.reasoning)
            if sum_reason > 0 and types.reasoning
            else 0.0
        )
        # If a type pool has $ but zero local tokens of that type, that pool is unallocated
        # (left on the floor) — rare; prefer keeping sum ≈ types.total via only positive bases.
        detail[b.key] = {
            "cached_usd": c,
            "input_usd": i,
            "output_usd": o,
            "reasoning_usd": r,
        }
        bill[b.key] = c + i + o + r

    return bill, detail


def list_price_usd(ticks: int) -> float:
    """Convert Build/API ``costUsdTicks`` to USD (1 USD = 10^10 ticks)."""
    return float(ticks) / float(TICKS_PER_USD)


def completion_tokens(
    *,
    output: int,
    reasoning: int,
    total: int = 0,
    input_tokens: int = 0,
) -> int:
    """Tokens billed at the output rate.

    Build logs ``outputTokens`` as completion+reasoning (input+output == total).
    Only add reasoning when the totals say it is extra.
    """
    out = max(int(output), 0)
    reason = max(int(reasoning), 0)
    if int(total) > 0 and abs(int(total) - (int(input_tokens) + out)) <= 1:
        return out
    return out + reason


def rates_for_prompt(rates: TokenRates, prompt_tokens: int) -> TokenRates:
    """2× list rates when the prompt reaches the published 200k long-context tier."""
    if int(prompt_tokens) < LONG_CONTEXT_PROMPT_TOKENS:
        return rates
    return TokenRates(
        uncached_input=rates.uncached_input * 2.0,
        cached_input=rates.cached_input * 2.0,
        output=rates.output * 2.0,
        label=f"{rates.label or 'rates'} ≥200k",
    )


def turn_list_usd(
    r: UsageRec | object,
    rates: TokenRates | None = None,
    *,
    prefer_ticks: bool = True,
) -> float:
    """list$ for one turn: ticks/1e10 (matches /usage Cost) or reconstructed rates."""
    ticks = int(getattr(r, "ticks", 0) or 0)
    if prefer_ticks and ticks > 0:
        return list_price_usd(ticks)
    cached = int(getattr(r, "cached", 0) or 0)
    inn = int(getattr(r, "input", 0) or 0)
    output = int(getattr(r, "output", 0) or 0)
    reasoning = int(getattr(r, "reasoning", 0) or 0)
    total = int(getattr(r, "total", 0) or 0)
    model = getattr(r, "model", None)
    base = rates or rates_for_model(str(model) if model else None)
    # Long-context 2× is already inside costUsdTicks. Do not apply it to
    # turn-aggregate input (a 1M-token turn may be many <200k calls).
    out_n = completion_tokens(output=output, reasoning=reasoning, total=total, input_tokens=inn)
    return api_estimate_usd(
        cached=cached,
        uncached_in=max(inn - cached, 0),
        output=out_n,
        reasoning=0,
        rates=base,
        reason_as_output=False,
    )


def parse_iso_date(s: str) -> date:
    return date.fromisoformat(s.strip()[:10])
