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

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
# grok-4.5 console Pricing (2026-08-05 screenshot): $2 / $0.30 / $6 (≤200k); $4 / $0.60 / $12 (>200k)
GROK_45_RATES = TokenRates(2.00, 0.30, 6.00, label="grok-4.5")
GROK_BUILD_RATES = TokenRates(1.00, 0.20, 2.00, label="grok-build-0.1")
GROK_43_RATES = TokenRates(1.25, 0.20, 2.50, label="grok-4.3")
GROK_420_RATES = TokenRates(1.25, 0.20, 2.50, label="grok-4.20")
GROK_4_RATES = TokenRates(3.00, 0.75, 15.00, label="grok-4")
GROK_3_RATES = TokenRates(3.00, 0.75, 15.00, label="grok-3")
GROK_3_MINI_RATES = TokenRates(0.30, 0.07, 0.50, label="grok-3-mini")

# Default cost model for estimates (user can override with --rates-model).
DEFAULT_RATES_MODEL = "grok-4.5"
DEFAULT_RATES = GROK_45_RATES

# Canonical profile names (what --rates-model accepts) → rates
RATE_PROFILES: dict[str, TokenRates] = {
    "grok-4.5": GROK_45_RATES,
    "4.5": GROK_45_RATES,
    "grok-4.5-build": GROK_45_RATES,  # Build often logs this id; use 4.5 list rates
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
        "grok-4.5",
        "grok-build-0.1",
        "grok-4.3",
        "grok-4.20",
        "grok-4",
        "grok-3",
        "grok-3-mini",
    ]
    return preferred


# Spend-oriented scale: list$ (pure API rates × local tokens) overstates prepaid burn.
# Measured: $60 prepaid − $16.46 remaining → burn $43.54 / list$ $76.97 ≈ 0.57
# (Aug 1–5 2026, grok-4.5 list rates, high cache). Tune via config or CLI.
DEFAULT_CASH_SCALE = 0.57


def apply_cash_scale(list_usd: float, scale: float) -> float:
    """est$ = list$ × scale (spend-oriented)."""
    if scale < 0:
        scale = 0.0
    return float(list_usd) * float(scale)


def effective_rates(rates: TokenRates, scale: float) -> TokenRates:
    """List rates × cash_scale — implied $/1M if burn were uniform across types."""
    s = max(0.0, float(scale))
    return TokenRates(
        uncached_input=rates.uncached_input * s,
        cached_input=rates.cached_input * s,
        output=rates.output * s,
        label=f"{rates.label or 'rates'}×{s:g}",
    )


def resolve_cash_scale(
    *,
    cli_scale: float | None = None,
    prepaid_usd: float | None = None,
    credits_remaining: float | None = None,
    list_total_usd: float | None = None,
    config_scale: float | None = None,
) -> tuple[float, str]:
    """Resolve cash scale and a short source label.

    Priority:
      1. --prepaid-usd + --credits-remaining + list_total → burn/list
      2. --cash-scale
      3. config cash_scale
      4. DEFAULT_CASH_SCALE
    """
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

    return DEFAULT_CASH_SCALE, f"default {DEFAULT_CASH_SCALE:g}"


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


def rates_for_model(model: str | None = None) -> TokenRates:
    """Resolve rates for a model id or profile name (fuzzy). Default: grok-4.5."""
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
        if name in key or key in name:
            return MODEL_RATES[name]
    return DEFAULT_RATES


def resolve_rates_model(rates_model: str | None) -> tuple[str, TokenRates]:
    """Return (canonical_label, rates) for --rates-model (default grok-4.5)."""
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
PRICES_LAST_VERIFIED = "2026-08-06"  # ISO date; bump when defaults re-checked
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


# Plain multi-line footer (print with markup=False — [brackets] are not Rich tags).
COST_CAVEATS_SHORT = """\
list$ = estimated $ at public API prices × your local tokens.
est$  = spend-style estimate (list$ × cash_scale; closer to prepaid burn).
Build Session Cost / Credits / Weekly limit are different meters — they will not match list$/est$.
FAQ: grok-utils usage info   ·   Tune: --cash-scale or config cash_scale"""

COST_CAVEATS_LONG = """
FAQ — Why numbers do not match Grok Build /usage
------------------------------------------------
Grok Build and grok-utils show different *kinds* of money and tokens. None is
"wrong"; they answer different questions.

  Q: What is list$ in the table?
  A: "If these tokens were billed at public API list prices for the model
     (--rates-model, default grok-4.5), what would that be?" Offline estimate
     from files under ~/.grok/sessions. Includes cache at the cached rate.

  Q: What is est$?
  A: Spend-oriented estimate: list$ × cash_scale. Default scale ~0.57 was
     measured against prepaid wallet burn (not against Session Cost). Closer
     to "how fast credits leave the wallet" than pure list$.

  Q: Why does Build Session Cost ($28) differ from list$ (~$18) on the same work?
  A: Session Cost is Build's own meter for *this session* (since start or last
     resume). list$ is public API rates × tokens. They use different accounting;
     Session Cost is often higher or lower than list$ and is NOT used to set
     cash_scale. Tokens can also differ slightly (UI snapshot vs full local log).

  Q: What are Credits, Weekly limit 100%, Auto topup $20?
  A: Account-wide wallet, not per-app:
       Credits     = prepaid balance still available
       Weekly limit= included pool for the period (100% = exhausted)
       Auto topup  = when the pool/credits need more, charge e.g. $20 at list-
                     style rates and add to Credits
     A top-up is not "this app cost $20"; it refills the shared wallet.

  Q: Which number should I trust for budgeting?
  A: - Per-app / per-day offline shares → grok-utils list$ / est$
     - "How expensive was this chat session?" → Build Session Cost
     - "How much money is left / did I top up?" → Credits + Auto topup
     - "Which plan if I keep this pace?" → usage cost --plan-advisor (-P)

  Q: Common commands (novice)
  A:  # This month-ish through latest local data
      grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5

      # Same + plan comparison (API vs SuperGrok vs Heavy)
      grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5 -P

      # Pin day-to-day scale in ~/.grok/grok-utils.toml
      [usage]
      cash_scale = 0.57

CAVEATS & COST LEDGERS (technical)
----------------------------------
Local meters (updates.jsonl turn_completed.usage):
  inputTokens, outputTokens, cachedReadTokens, reasoningTokens, costUsdTicks, modelCalls.
  Deduped by prompt_id (max totalTokens kept).

list$ formula (always includes cache):
  cached/1e6 × cached_rate + uncached_in/1e6 × input_rate
  + (output+reasoning)/1e6 × output_rate

est$ = list$ × cash_scale
  Default cash_scale ≈ 0.57 (API prepaid burn / list$ on measured windows).

  Day-to-day default (no CLI flags):
    # ~/.grok/grok-utils.toml  (or config.toml)
    [usage]
    cash_scale = 0.57

  Override for one run:
    --cash-scale N
    --prepaid-usd + --credits-remaining  (scale = wallet burn / list$ for the window)

Effective rates (list × scale) assume a *uniform* discount vs list.
  Prepaid may not discount cached/input/output equally — only wallet total is known offline.

--plan-advisor / -P
  Compare pure API vs SuperGrok vs SuperGrok Heavy for the same window
  (run-rate → monthly projection). "Best fit" assumes this window's intensity
  continues. If usage drops or varies a lot, pure API (est$) is often better —
  no flat subscription. Both SuperGrok tiers use a weekly usage pool + list-rate
  top-offs after 100%. Exact pool $ is not published — defaults are estimates.
  Heavy is priced for sustained high use; top-offs are a safety net.

  [usage]
  supergrok_usd = 30
  heavy_usd = 300
  supergrok_weekly_include_usd = 35
  heavy_weekly_include_usd = 150
  project_days = 30

MAINTAINING DEFAULTS (Phase 1 — no network in usage cost)
  When xAI announces API rate or SuperGrok/Heavy price changes:
    1. Update rate tables / plan constants in utils/pricing.py
    2. Bump PRICES_LAST_VERIFIED (ISO date)
    3. Or set overrides in ~/.grok/grok-utils.toml without a release
  Do not auto-scrape x.ai or call APIs from day-to-day cost reports (v1).

  Future (optional, opt-in): refresh list rates from Models API with a key:
    GET https://api.x.ai/v1/models  or  .../models/{model_id}
    Fields: prompt_text_token_price, cached_prompt_text_token_price,
            completion_text_token_price (+ long-context variants)
    Units: USD cents per 100M tokens → divide by 100 for $ per 1M.
  That updates list$ tables only — not subscription fees or weekly pool sizes
  (those still come from x.ai pricing announcements / human check).

--rates-model (default: grok-4.5) applies to list$ only.
  Standard context ≤200k; long-context 2× not applied yet.

Session UI Cost is lifetime of a session (may span SuperGrok + API eras).
Weekly limit / credits / auto-topup are account-global, not per-app.
""".strip()
