"""Model pricing for token-based pure-API cost estimates + plan-advisor defaults.

Rates are USD per 1M tokens (standard context ≤200k unless noted).

**Maintaining defaults (Phase 1 — current):** when xAI announces rate or plan
price changes, update the constants in this module and bump
``PRICES_LAST_VERIFIED``. Users can override plan-advisor numbers in
``~/.grok/grok-utils.toml`` without waiting for a release. Do **not** auto-
scrape or call the network from ``usage cost`` in v1.

**List rates (future optional refresh):** xAI Models API (Bearer API key):

  GET https://api.x.ai/v1/models
  GET https://api.x.ai/v1/models/{model_id}

Response pricing fields (USD **cents per 100M tokens**; ÷100 → $ per 1M):

  prompt_text_token_price, cached_prompt_text_token_price,
  completion_text_token_price (+ long-context variants)

That endpoint covers **API list rates**, not SuperGrok / Heavy **subscription**
fees or weekly pool sizes (still human-verified from x.ai pricing).

Verify also: console Pricing panel / https://docs.x.ai/developers/pricing.

These estimate "as if billed at pure API list rates" and are NOT SuperGrok
subscription cash and may not match a specific paygo key's effective mix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Re-export FAQ constants for older imports (canonical home: usage_faq.py)
from .usage_faq import COST_CAVEATS_LONG as COST_CAVEATS_LONG  # noqa: F401
from .usage_faq import COST_CAVEATS_SHORT as COST_CAVEATS_SHORT  # noqa: F401


@dataclass(frozen=True)
class TokenRates:
    """USD per 1M tokens (standard context tier)."""

    uncached_input: float
    cached_input: float
    output: float  # reasoning billed as output unless docs say otherwise
    label: str = ""  # human label for display

    def as_dict(self) -> dict[str, float | str]:
        return {
            "label": self.label,
            "uncached_input": self.uncached_input,
            "cached_input": self.cached_input,
            "output": self.output,
        }

    def short_label(self) -> str:
        name = self.label or "custom"
        return (
            f"{name}  (input ${self.uncached_input:.2f} / "
            f"cached ${self.cached_input:.2f} / out ${self.output:.2f} per 1M, ≤200k)"
        )


# --- List rates (USD / 1M), standard context (≤200k). Long-context 2× not applied in v1. ---
# docs.x.ai/developers/pricing (2026-08-13):
#   grok-4.6 ≤200k $2 / $0.50 / $6; ≥200k $4 / $1 / $12 (long-context not applied in v1)
#   grok-4.5 ≤200k $2 / $0.30 / $6; ≥200k $4 / $0.60 / $12
GROK_46_RATES = TokenRates(2.00, 0.50, 6.00, label="grok-4.6")
GROK_45_RATES = TokenRates(2.00, 0.30, 6.00, label="grok-4.5")
GROK_BUILD_RATES = TokenRates(1.00, 0.20, 2.00, label="grok-build-0.1")
GROK_43_RATES = TokenRates(1.25, 0.20, 2.50, label="grok-4.3")
GROK_420_RATES = TokenRates(1.25, 0.20, 2.50, label="grok-4.20")
GROK_4_RATES = TokenRates(3.00, 0.75, 15.00, label="grok-4")
GROK_3_RATES = TokenRates(3.00, 0.75, 15.00, label="grok-3")
GROK_3_MINI_RATES = TokenRates(0.30, 0.07, 0.50, label="grok-3-mini")

# Default cost model for estimates (user can override with --rates-model).
# grok-4.6 is the Grok Build default as of 2026-08-12.
DEFAULT_RATES_MODEL = "grok-4.6"
DEFAULT_RATES = GROK_46_RATES

# Canonical profile names (what --rates-model accepts) → rates
RATE_PROFILES: dict[str, TokenRates] = {
    "grok-4.6": GROK_46_RATES,
    "4.6": GROK_46_RATES,
    "grok-4.6-build": GROK_46_RATES,  # Build often logs this id; use 4.6 list rates
    "grok-4.5": GROK_45_RATES,
    "4.5": GROK_45_RATES,
    "grok-4.5-build": GROK_45_RATES,
    "grok-build-0.1": GROK_BUILD_RATES,
    "grok-build": GROK_BUILD_RATES,
    "build": GROK_BUILD_RATES,
    "grok-4.3": GROK_43_RATES,
    "4.3": GROK_43_RATES,
    "grok-4.20": GROK_420_RATES,
    "4.20": GROK_420_RATES,
    "grok-4": GROK_4_RATES,
    "grok-3": GROK_3_RATES,
    "grok-3-mini": GROK_3_MINI_RATES,
    "default": DEFAULT_RATES,
}

# Fuzzy match for session model ids (same objects as profiles)
MODEL_RATES: dict[str, TokenRates] = {
    "grok-4.6-build": GROK_46_RATES,
    "grok-4.6": GROK_46_RATES,
    "grok-4.5-build": GROK_45_RATES,
    "grok-4.5": GROK_45_RATES,
    "grok-build-0.1": GROK_BUILD_RATES,
    "grok-build": GROK_BUILD_RATES,
    "grok-4.3": GROK_43_RATES,
    "grok-4.20": GROK_420_RATES,
    "grok-4": GROK_4_RATES,
    "grok-3-mini": GROK_3_MINI_RATES,
    "grok-3": GROK_3_RATES,
    "default": DEFAULT_RATES,
}


def list_rate_profiles() -> list[str]:
    """Canonical profile names for help text (deduped, preferred order)."""
    preferred = [
        "grok-4.6",
        "grok-4.5",
        "grok-build-0.1",
        "grok-4.3",
        "grok-4.20",
        "grok-4",
        "grok-3",
        "grok-3-mini",
    ]
    return preferred


# Spend-oriented scales: est$ = list$ × scale.
# API path: full list rates (scale 1.0).
# SuperGrok included weekly pool: prepaid often barely moves (scale ~0).
# SuperGrok overage (weekly ~100%): extra credits burn ~1.9× list$ (measured Aug 2026).
# Blended 0.57 was multi-day pool+overage — kept only as optional legacy override.
DEFAULT_CASH_SCALE_API = 1.0
DEFAULT_CASH_SCALE_SUPERGROK_POOL = 0.0
DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE = 1.9
DEFAULT_TOPOFF_DISCOUNT = 0.0  # full pack price; set 0.20/0.25/0.40 only during promo
# Ultimate fallback if auth unknown
DEFAULT_CASH_SCALE = DEFAULT_CASH_SCALE_API
# Weekly usage % at/above this → treat SuperGrok as overage (extra credits)
OVERAGE_WEEKLY_PCT_THRESHOLD = 99.0


def apply_cash_scale(list_usd: float, scale: float) -> float:
    """est$ = list$ × scale (spend-oriented)."""
    if scale < 0:
        scale = 0.0
    return float(list_usd) * float(scale)


def apply_topoff_discount(credit_usd: float, discount: float) -> float:
    """Card $ ≈ credit face $ × (1 − discount). discount 0 = full price, 1.0 = free tops."""
    d = clamp_topoff_discount(discount)
    return float(credit_usd) * (1.0 - d)


def clamp_topoff_discount(discount: float) -> float:
    """Promo fraction in [0, 1]. 1.0 = free Extra Credits top-ups (card $0)."""
    return min(max(float(discount), 0.0), 1.0)


# Offered pack promos for plan-advisor rows (not full price; not free 100%).
DEFAULT_TOPOFF_DISCOUNT_SCENARIOS: tuple[float, ...] = (0.20, 0.25, 0.40)
# Extra Credits packs for card math (round top face up to whole packs).
DEFAULT_TOPOFF_PACK_USD = 100.0


def ceil_to_pack_usd(face_usd: float, pack_usd: float = DEFAULT_TOPOFF_PACK_USD) -> float:
    """Round face credit need up to whole pack sizes (e.g. $380 → $400 @ $100 packs)."""
    face = max(0.0, float(face_usd))
    pack = max(0.0, float(pack_usd))
    if face <= 0 or pack <= 0:
        return face
    import math

    return float(math.ceil(face / pack - 1e-12) * pack)


def resolve_topoff_pack_usd(usage_cfg: dict[str, Any] | None = None) -> float:
    cfg = usage_cfg or {}
    return max(0.0, cfg_float(cfg, "topoff_pack_usd", DEFAULT_TOPOFF_PACK_USD))


def topoff_discount_label(discount: float) -> str:
    """Human label for a top-off promo fraction."""
    d = clamp_topoff_discount(discount)
    if d <= 0:
        return "full price tops"
    if d >= 1.0 - 1e-12:
        return "promo −100% (free tops)"
    pct = int(round(d * 100))
    return f"promo −{pct}%"


def effective_rates(rates: TokenRates, scale: float) -> TokenRates:
    """List rates × cash_scale — implied $/1M if burn were uniform across types."""
    s = max(0.0, float(scale))
    return TokenRates(
        uncached_input=rates.uncached_input * s,
        cached_input=rates.cached_input * s,
        output=rates.output * s,
        label=f"{rates.label or 'rates'}×{s:g}",
    )


def cfg_float(cfg: dict[str, Any], key: str, default: float) -> float:
    """Parse a float from usage config with a default."""
    raw = cfg.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


# Back-compat alias (older call sites)
_cfg_float = cfg_float


def resolve_cash_scale(
    *,
    cli_scale: float | None = None,
    prepaid_usd: float | None = None,
    credits_remaining: float | None = None,
    list_total_usd: float | None = None,
    config_scale: float | None = None,
    usage_cfg: dict[str, Any] | None = None,
    auth_effective: str | None = None,
    weekly_usage_pct: float | None = None,
    regime_override: str | None = None,
) -> tuple[float, str]:
    """Resolve cash scale and a short source label.

    Priority:
      1. --prepaid-usd + --credits-remaining + list_total → burn/list
      2. --cash-scale
      3. config cash_scale (force one number)
      4. path + SuperGrok regime from auth / weekly % / toml
      5. DEFAULT_CASH_SCALE_API (1.0)
    """
    cfg = usage_cfg or {}

    if (
        prepaid_usd is not None
        and credits_remaining is not None
        and list_total_usd is not None
        and list_total_usd > 0
    ):
        burn = float(prepaid_usd) - float(credits_remaining)
        if burn < 0:
            burn = 0.0
        scale = burn / float(list_total_usd)
        return scale, f"wallet burn ${burn:.2f} / list$ ${list_total_usd:.2f}"

    if cli_scale is not None:
        return float(cli_scale), f"cli --cash-scale {cli_scale:g}"

    if config_scale is not None:
        return float(config_scale), f"config cash_scale={config_scale:g}"

    # Explicit single key in toml already handled via config_scale by callers;
    # also accept if passed inside usage_cfg only:
    if "cash_scale" in cfg and cfg.get("cash_scale") is not None:
        try:
            v = float(cfg["cash_scale"])
            return v, f"config cash_scale={v:g}"
        except (TypeError, ValueError):
            pass

    scale_api = _cfg_float(cfg, "cash_scale_api", DEFAULT_CASH_SCALE_API)
    scale_pool = _cfg_float(cfg, "cash_scale_supergrok_pool", DEFAULT_CASH_SCALE_SUPERGROK_POOL)
    scale_ov = _cfg_float(cfg, "cash_scale_supergrok_overage", DEFAULT_CASH_SCALE_SUPERGROK_OVERAGE)

    regime = (regime_override or str(cfg.get("supergrok_regime") or "auto")).lower().strip()
    auth = (auth_effective or "none").lower().strip()

    if auth == "api_key":
        return scale_api, f"api_key scale={scale_api:g}"

    if auth == "supergrok_session":
        use_overage = False
        if regime in ("overage", "extra", "credits"):
            use_overage = True
            why = "toml supergrok_regime=overage"
        elif regime in ("pool", "included"):
            use_overage = False
            why = "toml supergrok_regime=pool"
        else:
            # auto: weekly usage % from billing log (at turn time when available)
            thr = _cfg_float(cfg, "overage_weekly_pct_threshold", OVERAGE_WEEKLY_PCT_THRESHOLD)
            if weekly_usage_pct is not None and weekly_usage_pct >= thr:
                use_overage = True
                why = f"weekly {weekly_usage_pct:g}%≥{thr:g}% overage"
            elif weekly_usage_pct is not None:
                use_overage = False
                why = f"weekly {weekly_usage_pct:g}% pool"
            else:
                # Weekly % unknown (pre-log / no billing sample yet): do not invent
                # pool 0 or overage 1.9 — use list$ scale + label as unknown regime.
                return (
                    scale_api,
                    f"supergrok weekly % unknown → scale={scale_api:g} (list$; regime unknown)",
                )
        if use_overage:
            return scale_ov, f"supergrok overage scale={scale_ov:g} ({why})"
        return scale_pool, f"supergrok pool scale={scale_pool:g} ({why})"

    return scale_api, f"default api scale={scale_api:g} (auth={auth or 'none'})"


def resolve_topoff_discount(
    usage_cfg: dict[str, Any] | None = None,
    *,
    cli_discount: float | None = None,
) -> tuple[float, str]:
    """Pack promo discount: 0 = full price. Opt-in 0.25 / 1.0 etc. for modeling only."""
    if cli_discount is not None:
        d = clamp_topoff_discount(cli_discount)
        if d <= 0:
            return 0.0, "full price (cli --topoff-discount 0)"
        return d, f"cli --topoff-discount {d:g} ({topoff_discount_label(d)}; card≈face×{1 - d:g})"
    cfg = usage_cfg or {}
    d = clamp_topoff_discount(_cfg_float(cfg, "topoff_discount", DEFAULT_TOPOFF_DISCOUNT))
    if d <= 0:
        return 0.0, "full price (topoff_discount=0)"
    return d, f"topoff_discount={d:g} ({topoff_discount_label(d)}; card≈face×{1 - d:g})"


def resolve_topoff_discount_scenarios(
    usage_cfg: dict[str, Any] | None = None,
    *,
    active_discount: float | None = None,
) -> list[float]:
    """Offered pack promo fractions for plan-advisor rows (default −20/−25/−40%).

    Full price is the base SuperGrok/Heavy table rows, not a scenario.
    Optional toml: topoff_discount_scenarios = [0.20, 0.25, 0.40]
    Active --topoff-discount is appended when set and not already listed.
    Zero discounts are dropped (redundant with full-price rows).
    """
    cfg = usage_cfg or {}
    raw = cfg.get("topoff_discount_scenarios")
    out: list[float] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            try:
                out.append(clamp_topoff_discount(float(item)))
            except (TypeError, ValueError):
                continue
    if not out:
        out = list(DEFAULT_TOPOFF_DISCOUNT_SCENARIOS)
    # Dedupe while preserving order; drop full-price 0 (base rows cover that)
    seen: set[float] = set()
    uniq: list[float] = []
    for d in out:
        if d <= 0:
            continue
        key = round(d, 6)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    if active_discount is not None:
        ad = clamp_topoff_discount(active_discount)
        if ad > 0:
            key = round(ad, 6)
            if key not in seen:
                uniq.append(ad)
    return uniq


@dataclass
class AuthMixSlice:
    """list$/est$ for one auth path within a window."""

    path: str  # api_key | supergrok_session | unknown
    list_usd: float
    est_usd: float
    scale: float
    scale_src: str
    tokens: int
    prompts: int
    cached: int = 0

    @property
    def cache_pct(self) -> float:
        if self.tokens <= 0:
            return 0.0
        return 100.0 * float(self.cached) / float(self.tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "list_usd": round(self.list_usd, 4),
            "est_usd": round(self.est_usd, 4),
            "scale": self.scale,
            "scale_src": self.scale_src,
            "tokens": self.tokens,
            "prompts": self.prompts,
            "cached": self.cached,
            "cache_pct": round(self.cache_pct, 2),
        }


@dataclass
class AuthMixResult:
    """Window est$ split by auth timeline (from unified.jsonl change-points)."""

    slices: list[AuthMixSlice]
    list_total: float
    est_total: float
    source: str  # "auth_mix" | "uniform"
    uniform_scale: float | None = None
    uniform_src: str | None = None
    # Optional one-pass bucket totals when group_key_fn was provided
    est_by_key: dict[str, float] = field(default_factory=dict)
    list_by_key: dict[str, float] = field(default_factory=dict)
    # key → path → list$ (regime breakdown per app/bucket)
    list_by_key_path: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # Reconciled list% on slices for consumers that sum to 100
        weights = [s.list_usd for s in self.slices]
        total_w = sum(weights) or 1.0
        raw = [100.0 * w / total_w for w in weights]
        floors = [int(x) for x in raw]
        rem = 100 - sum(floors)
        order = sorted(
            range(len(raw)),
            key=lambda i: (raw[i] - floors[i], i),
            reverse=True,
        )
        pcts = floors[:]
        for i in order[: max(0, rem)]:
            pcts[i] += 1
        slices_out = []
        for s, pct in zip(self.slices, pcts, strict=True):
            d = s.as_dict()
            d["list_pct"] = pct
            slices_out.append(d)
        return {
            "source": self.source,
            "list_total": round(self.list_total, 4),
            "est_total": round(self.est_total, 4),
            "uniform_scale": self.uniform_scale,
            "uniform_src": self.uniform_src,
            "slices": slices_out,
            "est_by_key": {k: round(v, 4) for k, v in self.est_by_key.items()},
            "list_by_key": {k: round(v, 4) for k, v in self.list_by_key.items()},
            "list_by_key_path": {
                k: {p: round(v, 4) for p, v in paths.items()}
                for k, paths in self.list_by_key_path.items()
            },
        }


def estimate_with_auth_mix(
    records: list[Any],
    rates: TokenRates,
    *,
    change_points: list[Any],
    usage_cfg: dict[str, Any] | None = None,
    weekly_usage_pct: float | None = None,
    weekly_timeline: list[Any] | None = None,
    tier_timeline: list[Any] | None = None,
    fallback_auth: str = "unknown",
    force_uniform_scale: float | None = None,
    force_uniform_src: str | None = None,
    group_key_fn: Any | None = None,
    prefer_ticks: bool = True,
) -> AuthMixResult:
    """Apply path-specific scales using auth change-points (or one forced scale).

    When force_uniform_scale is set (--cash-scale / prepaid fit), all turns use it.
    Otherwise each turn is labeled from the log timeline and scaled by path/regime.

    SuperGrok/Heavy pool vs overage uses **weekly % at turn time** from billing
    log when available — not "current week only" (which zeroed historical
    top-off burn). Turns before the first sample reuse that sample when it is
    still in-pool and within one week (truncated ``unified.jsonl``). Otherwise
    missing weekly % → list$ scale (regime unknown). ``subscriptionTier``
    relabels SuperGrok slices as Heavy when the log says so at turn time.

    Optional group_key_fn(record) → str accumulates est$/list$ per key in one pass
    (avoids re-running mix per report bucket).
    """
    from .auth_status import (  # local import avoids cycle issues
        auth_effective_at,
        subscription_tier_at,
        weekly_usage_at,
    )
    from .usage_tokens import turn_list_usd

    cfg = usage_cfg or {}
    list_total = 0.0
    # accumulate by path; track scale range for display
    acc: dict[str, dict[str, float | int | str]] = {}
    est_by_key: dict[str, float] = {}
    list_by_key: dict[str, float] = {}
    list_by_key_path: dict[str, dict[str, float]] = {}

    for r in records:
        list_b = float(turn_list_usd(r, rates, prefer_ticks=prefer_ticks))
        list_total += list_b
        ts = getattr(r, "ts", None)
        if force_uniform_scale is not None:
            path = "uniform"
            scale = float(force_uniform_scale)
            scale_src = force_uniform_src or f"uniform {scale:g}"
        else:
            if ts is not None and change_points:
                path = auth_effective_at(ts, change_points, fallback=fallback_auth)
            else:
                path = fallback_auth
            # Per-turn weekly % when timeline exists. None only if before the
            # first sample *and* in-pool holdback does not apply.
            w_pct = weekly_usage_pct
            if ts is not None and weekly_timeline:
                w_pct = weekly_usage_at(ts, weekly_timeline)
            auth_for_scale = path if path != "unknown" else fallback_auth
            if path == "unknown":
                scale, scale_src = resolve_cash_scale(
                    usage_cfg=cfg,
                    auth_effective="api_key",
                    weekly_usage_pct=w_pct,
                )
                scale_src = f"unknown→{scale_src}"
            else:
                scale, scale_src = resolve_cash_scale(
                    usage_cfg=cfg,
                    auth_effective=auth_for_scale,
                    weekly_usage_pct=w_pct,
                )
            # Split SuperGrok/Heavy by regime so pool/overage/unknown don't collapse
            if path == "supergrok_session":
                if "unknown" in scale_src or w_pct is None:
                    path = "supergrok_unknown"
                elif scale <= 0.05:
                    path = "supergrok_pool"
                elif scale >= 1.5:
                    path = "supergrok_overage"
                # else mid-scale custom → keep supergrok_session
                if ts is not None and tier_timeline:
                    tier = subscription_tier_at(ts, tier_timeline)
                    if tier == "heavy" and path.startswith("supergrok_"):
                        path = "heavy_" + path[len("supergrok_") :]

        est_b = list_b * scale
        if group_key_fn is not None:
            try:
                gkey = str(group_key_fn(r))
            except (TypeError, ValueError, AttributeError, KeyError):
                gkey = "?"
            est_by_key[gkey] = est_by_key.get(gkey, 0.0) + est_b
            list_by_key[gkey] = list_by_key.get(gkey, 0.0) + list_b
            path_map = list_by_key_path.setdefault(gkey, {})
            path_map[path] = path_map.get(path, 0.0) + list_b

        bucket = acc.setdefault(
            path,
            {
                "list_usd": 0.0,
                "est_usd": 0.0,
                "scale": scale,
                "scale_src": scale_src,
                "scale_min": scale,
                "scale_max": scale,
                "tokens": 0,
                "prompts": 0,
                "cached": 0,
            },
        )
        bucket["list_usd"] = float(bucket["list_usd"]) + list_b
        bucket["est_usd"] = float(bucket["est_usd"]) + est_b
        bucket["tokens"] = int(bucket["tokens"]) + int(getattr(r, "total", 0) or 0)
        bucket["cached"] = int(bucket["cached"]) + int(getattr(r, "cached", 0) or 0)
        bucket["prompts"] = int(bucket["prompts"]) + 1
        bucket["scale"] = scale
        bucket["scale_src"] = scale_src
        bucket["scale_min"] = min(float(bucket["scale_min"]), scale)
        bucket["scale_max"] = max(float(bucket["scale_max"]), scale)

    slices = []
    for p, v in sorted(acc.items(), key=lambda kv: -float(kv[1]["list_usd"])):
        smin, smax = float(v["scale_min"]), float(v["scale_max"])
        # Effective average scale for the slice (est/list)
        list_u = float(v["list_usd"])
        est_u = float(v["est_usd"])
        avg = (est_u / list_u) if list_u > 0 else float(v["scale"])
        src = str(v["scale_src"])
        if abs(smax - smin) > 0.05:
            src = f"mixed scales {smin:g}–{smax:g} (avg {avg:.2f})"
        slices.append(
            AuthMixSlice(
                path=p,
                list_usd=list_u,
                est_usd=est_u,
                scale=avg,
                scale_src=src,
                tokens=int(v["tokens"]),
                prompts=int(v["prompts"]),
                cached=int(v.get("cached") or 0),
            )
        )
    est_total = sum(s.est_usd for s in slices)
    if force_uniform_scale is not None:
        return AuthMixResult(
            slices=slices,
            list_total=list_total,
            est_total=est_total,
            source="uniform",
            uniform_scale=float(force_uniform_scale),
            uniform_src=force_uniform_src,
            est_by_key=est_by_key,
            list_by_key=list_by_key,
            list_by_key_path=list_by_key_path,
        )
    return AuthMixResult(
        slices=slices,
        list_total=list_total,
        est_total=est_total,
        source="auth_mix" if change_points else "fallback_auth",
        uniform_scale=None,
        uniform_src=None,
        est_by_key=est_by_key,
        list_by_key=list_by_key,
        list_by_key_path=list_by_key_path,
    )


def load_usage_config(grok_home: Path | str) -> dict[str, Any]:
    """Load optional [usage] section from grok-utils.toml or config.toml."""
    from .common import load_toml

    home = Path(grok_home)
    for name in ("grok-utils.toml", "config.toml"):
        path = home / name
        data = load_toml(path)
        if not data:
            continue
        usage = data.get("usage")
        if isinstance(usage, dict):
            return usage
    return {}


def _model_id_hit(needle: str, haystack: str) -> bool:
    """Substring match that does not let grok-4 steal grok-4.6.

    A hit must not be preceded by a digit/dot, and must not be followed by
    ``.`` + digit (version continuation). So ``grok-4`` matches ``grok-4-0709``
    but not ``grok-4.6`` / ``grok-4.6-build``.
    """
    if not needle or needle == "default":
        return False
    pat = rf"(?<![\d.]){re.escape(needle)}(?!\.\d)"
    return re.search(pat, haystack) is not None


def rates_for_model(model: str | None = None) -> TokenRates:
    """Resolve rates for a model id or profile name (fuzzy). Default: grok-4.6."""
    if not model:
        return DEFAULT_RATES
    key = model.lower().strip()
    if key in RATE_PROFILES:
        return RATE_PROFILES[key]
    if key in MODEL_RATES:
        return MODEL_RATES[key]
    for name in sorted(MODEL_RATES.keys(), key=len, reverse=True):
        if name == "default":
            continue
        if _model_id_hit(name, key) or _model_id_hit(key, name):
            return MODEL_RATES[name]
    return DEFAULT_RATES


def resolve_rates_model(rates_model: str | None) -> tuple[str, TokenRates]:
    """Return (canonical_label, rates) for --rates-model (default grok-4.6)."""
    if not rates_model or not rates_model.strip():
        return DEFAULT_RATES_MODEL, DEFAULT_RATES
    key = rates_model.lower().strip()
    rates = rates_for_model(key)
    # Prefer clean profile label
    label = rates.label or key
    return label, rates


def api_estimate_usd(
    *,
    cached: int,
    uncached_in: int,
    output: int,
    reasoning: int,
    rates: TokenRates | None = None,
    reason_as_output: bool = True,
) -> float:
    """Pure-API cost from token splits (includes cached at cached_input rate)."""
    r = rates or DEFAULT_RATES
    out_tokens = output + (reasoning if reason_as_output else 0)
    return (
        cached / 1e6 * r.cached_input
        + uncached_in / 1e6 * r.uncached_input
        + out_tokens / 1e6 * r.output
    )


# --- Plan advisor (subscription vs pure API) ---------------------------------
# SuperGrok and Heavy share weekly pool + list-rate top-offs after 100%.
# Exact pool $ is not published; defaults are estimates (override in toml).
# When xAI announces plan price changes: update these + PRICES_LAST_VERIFIED.
PRICES_LAST_VERIFIED = "2026-08-13"  # ISO date; bump when defaults re-checked
DEFAULT_SUPERGROK_USD = 30.0
DEFAULT_HEAVY_USD = 300.0
DEFAULT_SUPERGROK_WEEKLY_INCLUDE_USD = 35.0  # small — heavy Build exhausts fast
DEFAULT_HEAVY_WEEKLY_INCLUDE_USD = 150.0  # large — tops rare for coding/Build
DEFAULT_PROJECT_DAYS = 30


@dataclass(frozen=True)
class SubPlanCost:
    """Subscription + list-rate overage after modeled weekly include."""

    name: str
    sub_usd: float
    weekly_include_usd: float
    included_monthly: float
    overage_list_usd: float
    monthly: float

    def as_dict(self) -> dict[str, float | str]:
        return {
            "name": self.name,
            "sub_usd": round(self.sub_usd, 4),
            "weekly_include_usd": round(self.weekly_include_usd, 4),
            "included_monthly": round(self.included_monthly, 4),
            "overage_list_usd": round(self.overage_list_usd, 4),
            "monthly": round(self.monthly, 4),
        }


@dataclass(frozen=True)
class PlanAdvisorResult:
    """Break-even projection for pure API vs SuperGrok vs Heavy."""

    window_days: int
    project_days: int
    list_usd: float
    est_usd: float
    tokens: int
    cache_pct: float
    cash_scale: float
    daily_list: float
    daily_est: float
    daily_tokens: float
    api_list_monthly: float
    api_est_monthly: float
    tokens_monthly: float
    supergrok: SubPlanCost
    heavy: SubPlanCost
    winner: str  # api_est | supergrok | heavy
    winner_monthly: float
    save_vs_api_est: float
    heavy_breakeven_tokens_monthly: float | None
    heavy_cheaper_than_list_api: bool

    def as_dict(self) -> dict:
        return {
            "window_days": self.window_days,
            "project_days": self.project_days,
            "list_usd": round(self.list_usd, 4),
            "est_usd": round(self.est_usd, 4),
            "tokens": self.tokens,
            "cache_pct": round(self.cache_pct, 2),
            "cash_scale": self.cash_scale,
            "daily_list": round(self.daily_list, 4),
            "daily_est": round(self.daily_est, 4),
            "daily_tokens": round(self.daily_tokens, 1),
            "api_list_monthly": round(self.api_list_monthly, 4),
            "api_est_monthly": round(self.api_est_monthly, 4),
            "tokens_monthly": round(self.tokens_monthly, 1),
            "supergrok": self.supergrok.as_dict(),
            "heavy": self.heavy.as_dict(),
            "winner": self.winner,
            "winner_monthly": round(self.winner_monthly, 4),
            "save_vs_api_est": round(self.save_vs_api_est, 4),
            "heavy_breakeven_tokens_monthly": (
                round(self.heavy_breakeven_tokens_monthly, 1)
                if self.heavy_breakeven_tokens_monthly is not None
                else None
            ),
            "heavy_cheaper_than_list_api": self.heavy_cheaper_than_list_api,
            "assumptions": {
                "topoffs_at_list_rates": True,
                "weekly_pool_sizes_estimated": True,
                "heavy_tops_are_safety_net": True,
                "winner_assumes_intensity_holds": True,
                "variable_or_lower_usage_favors_api": True,
            },
        }


def _sub_plus_topoff(
    *,
    name: str,
    sub_usd: float,
    weekly_include_usd: float,
    api_list_monthly: float,
    weeks_in_month: float,
) -> SubPlanCost:
    include = max(0.0, float(weekly_include_usd)) * max(0.0, float(weeks_in_month))
    overage = max(0.0, float(api_list_monthly) - include)
    return SubPlanCost(
        name=name,
        sub_usd=float(sub_usd),
        weekly_include_usd=float(weekly_include_usd),
        included_monthly=include,
        overage_list_usd=overage,
        monthly=float(sub_usd) + overage,
    )


def plan_advisor(
    *,
    list_usd: float,
    est_usd: float,
    tokens: int,
    cache_pct: float,
    cash_scale: float,
    window_days: int,
    project_days: int = DEFAULT_PROJECT_DAYS,
    supergrok_usd: float = DEFAULT_SUPERGROK_USD,
    heavy_usd: float = DEFAULT_HEAVY_USD,
    supergrok_weekly_include_usd: float = DEFAULT_SUPERGROK_WEEKLY_INCLUDE_USD,
    heavy_weekly_include_usd: float = DEFAULT_HEAVY_WEEKLY_INCLUDE_USD,
) -> PlanAdvisorResult:
    """Project monthly cost of pure API vs SuperGrok vs Heavy for a usage window."""
    days = max(1, int(window_days))
    month = max(1, int(project_days))
    weeks = month / 7.0

    daily_list = float(list_usd) / days
    daily_est = float(est_usd) / days
    daily_tok = float(tokens) / days

    api_list_mo = daily_list * month
    api_est_mo = daily_est * month
    tok_mo = daily_tok * month

    sg = _sub_plus_topoff(
        name="supergrok",
        sub_usd=supergrok_usd,
        weekly_include_usd=supergrok_weekly_include_usd,
        api_list_monthly=api_list_mo,
        weeks_in_month=weeks,
    )
    hv = _sub_plus_topoff(
        name="heavy",
        sub_usd=heavy_usd,
        weekly_include_usd=heavy_weekly_include_usd,
        api_list_monthly=api_list_mo,
        weeks_in_month=weeks,
    )

    # Primary winner: est$ API vs subscription totals (overage already at list)
    candidates = {
        "api_est": api_est_mo,
        "supergrok": sg.monthly,
        "heavy": hv.monthly,
    }
    winner = min(candidates, key=lambda k: candidates[k])
    winner_mo = candidates[winner]
    save = api_est_mo - winner_mo

    be_tok: float | None = None
    if tokens > 0 and list_usd > 0:
        list_per_token = float(list_usd) / float(tokens)
        if list_per_token > 0:
            be_tok = float(heavy_usd) / list_per_token

    return PlanAdvisorResult(
        window_days=days,
        project_days=month,
        list_usd=float(list_usd),
        est_usd=float(est_usd),
        tokens=int(tokens),
        cache_pct=float(cache_pct),
        cash_scale=float(cash_scale),
        daily_list=daily_list,
        daily_est=daily_est,
        daily_tokens=daily_tok,
        api_list_monthly=api_list_mo,
        api_est_monthly=api_est_mo,
        tokens_monthly=tok_mo,
        supergrok=sg,
        heavy=hv,
        winner=winner,
        winner_monthly=winner_mo,
        save_vs_api_est=save,
        heavy_breakeven_tokens_monthly=be_tok,
        heavy_cheaper_than_list_api=api_list_mo > float(heavy_usd),
    )


def load_plan_advisor_config(usage_cfg: dict | None) -> dict[str, float]:
    """Extract plan-advisor overrides from [usage] config dict."""
    cfg = usage_cfg or {}
    out: dict[str, float] = {}
    keys = {
        "supergrok_usd": DEFAULT_SUPERGROK_USD,
        "heavy_usd": DEFAULT_HEAVY_USD,
        "supergrok_weekly_include_usd": DEFAULT_SUPERGROK_WEEKLY_INCLUDE_USD,
        "heavy_weekly_include_usd": DEFAULT_HEAVY_WEEKLY_INCLUDE_USD,
        "project_days": DEFAULT_PROJECT_DAYS,
    }
    for key, default in keys.items():
        raw = cfg.get(key, default)
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            out[key] = float(default)
    return out
