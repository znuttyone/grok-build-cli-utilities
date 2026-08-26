# usage

Beautiful usage analytics and **token-based cost estimates** (local sessions only).

```bash
grok-utils usage --help
grok-utils usage info    # FAQ + ledgers (read this if $ numbers confuse you)
```

## FAQ (novice) — Build `/usage` vs grok-utils

Grok Build and this tool show **different meters**. They will **not** match dollar-for-dollar.

| What you see | Where | Plain English | Affects table? |
|---|---|---|---|
| **Session Cost $…** | Build `/usage` | This chat session’s own meter | **No** — not used in list$/est$ |
| **list$** | `usage cost` | **Build `costUsdTicks` ÷ 10^10** (same $ as `/usage` Session Cost). Pass `-m` to reconstruct from a published rate table instead. | **Primary activity column** |
| **Cache%** | same table | Share of input billed at the *cheaper cached* rate (context reuse). High cache% → lower list$ for the same tokens | **Yes** — part of list$ formula |
| **est$** | same table | Spend lens: API ≈ list$; SuperGrok/Heavy pool ≈ **0** Extra Credits; overage ≈ **1.9×** list$ | **Yes** — scale on list$ only |
| **Extra Credits $** | Build UI / `auth status` / footer | Wallet balance left | **No** — snapshot only (not subtracted from list$/est$) |
| **Weekly limit %** | Build UI / footer | Included SuperGrok or Heavy pool used | **Only est$ regime** (pool vs overage). Not list$ |
| **Auto topup $20** | Build UI | Card charge to *refill* Extra Credits | **No** — not “this app cost $20” and not in the table |
| **Auth path** | `auth status` / footer | SuperGrok or Heavy session vs API key | **Yes for est$** (path/mix). Not list$ |
| **Plan (SuperGrok vs Heavy)** | billing log / footer / `-P` | `ctx.subscriptionTier` on billing fetch lines | Labels mix + planner; not list$ |
| **Wallet / auth line** | `usage cost` / `usage report` footer | `Extra Credits $… · weekly N% · Heavy session` plus **Weekly Heavy pool resets …** | Snapshot only — see FAQ |

**Auth matters for spend:** SuperGrok/Heavy session wins over `XAI_API_KEY` unless `preferred_method = "api_key"`. Wallet + plan come from billing lines in `logs/unified.jsonl` (`prepaidBalance`, `creditUsagePercent`, `currentPeriod.end`, `subscriptionTier`) — not from `/usage` turn files. The Build `/usage` panel may still say SuperGrok after you upgrade. `resets` is `currentPeriod.end` in local time (same clock as `/usage` “Resets”).

**What to use when**

| Goal | Use |
|---|---|
| Which app/day used the most? | `usage cost --by app` or `--by day` → **list$** |
| Extra-credit burn estimate | **est$** (regime-aware) or re-fit with `--prepaid-usd` + `--credits-remaining` |
| “How heavy was this session?” | Build **Session Cost** |
| “How much is left / weekly pool?” | `auth status` or cost footer wallet snapshot |
| API vs SuperGrok vs Heavy? | `usage cost … --plan-advisor` (or `-P`) — if intensity **holds** |
| Model promo −25% / free tops | `--topoff-discount 0.25` or `1.0` (+ plan-advisor scenarios) |

```bash
# Typical day-to-day
grok-utils usage cost --from 2026-08-01 --by app -m grok-4.6

# Plan comparison (compact; soft recommendation if run-rate continues)
grok-utils usage cost --from 2026-07-18 --by app -m grok-4.6 -P

# Slightly more plan detail (promo table + overage one-liner; FAQ still in usage info)
grok-utils usage cost ... -P --detail

# Model pack promo for est_cash$ (best-fit still full price)
grok-utils usage cost ... -P --topoff-discount 0.25

# Wallet snapshot
grok-utils auth status
```

Same FAQ text is printed by:

```bash
grok-utils usage info
```

## cost (primary)

Estimate from `~/.grok/sessions` turn usage (list rates + spend-oriented scale).

```bash
# Closed window
grok-utils usage cost --from 2026-08-01 --to 2026-08-05 --by app -m grok-4.6

# From a date through latest session data (omit --to; title shows … for open end)
grok-utils usage cost --from 2026-08-01 --by app -m grok-4.6

# Fit est$ to wallet burn for a window (one-shot recalibration)
# e.g. start ~$10 + tops $60 − remaining $21.40 → --prepaid-usd 70 --credits-remaining 21.40
grok-utils usage cost --from 2026-08-01 --by app \
  --prepaid-usd 70 --credits-remaining 21.40

# Force one uniform scale for a window (optional legacy blend; disables path split)
grok-utils usage cost ... --cash-scale 0.69

# Plan advisor: pure API vs SuperGrok vs SuperGrok Heavy (run-rate → monthly)
grok-utils usage cost --from 2026-07-18 --by app -m grok-4.6 --plan-advisor
# short flag: -P

# Model top-off promo for est_cash$ + plan-advisor scenario table
grok-utils usage cost ... -P --topoff-discount 0.25
```

| Column | Meaning |
|---|---|
| **list$** | `costUsdTicks ÷ 10^10` when present (matches `/usage` Cost). With `-m`, tokens × that model's published ≤200k rates. |
| **est$** | **list$ × path/regime scale** — Extra Credits burn lens (pool ≈ 0, overage ≈ 1.9×). |
| **est_cash$** | When promo set: est$ × (1 − topoff_discount) — card $ on tops. |

### Cash scale (path/regime defaults)

Built-in (unless you force a single number):

| Path / regime | Default scale |
|---|---|
| API key | **1.0** |
| SuperGrok / Heavy pool (weekly &lt; ~99%) | **0.0** |
| SuperGrok / Heavy overage (weekly ~100%) | **1.9** |
| Before first in-pool sample (≤7d; truncated log) | **0.0** (inferred same-week pool) |
| SuperGrok / Heavy weekly% unknown | **1.0** + caveat |

**Force priority** (disables path split when set):

1. `--prepaid-usd` + `--credits-remaining` → `scale = (prepaid − remaining) / list$`
2. `--cash-scale N` (this run only)
3. Config `[usage] cash_scale` (optional legacy single number, e.g. multi-day blend)

```toml
# ~/.grok/grok-utils.toml
[usage]
cash_scale_api = 1.0
cash_scale_supergrok_pool = 0.0
cash_scale_supergrok_overage = 1.9
topoff_discount = 0.0                   # 0 full price; 0.25 / 1.0 to model promo
topoff_discount_scenarios = [0.20, 0.25, 0.40]
```

### Options

| Flag | Meaning |
|---|---|
| `--from` / `--to` / `--since` | Inclusive dates; omit `--to` for through **latest** session data (`--since` = `--from`). If `--from` is earlier than any turn in the logs, a warning shows the real earliest date (table title uses the data span). |
| `--by` | `app` \| `project` \| `model` \| `day` \| `week` \| `month` |
| `-m` / `--rates-model` | Force a reconstructed rate table for **list$** (ignores ticks). Omit to use `/usage` Session Cost (`costUsdTicks÷1e10`). Fallback table: `grok-4.6` |
| `--cash-scale` | Force uniform list$ → est$ scale (else path/regime defaults) |
| `--prepaid-usd` / `--credits-remaining` | Set scale from wallet burn |
| `--topoff-discount` | `0..1` pack promo for est_cash$ / plan scenarios (`1.0` = free tops) |
| `--api-estimate` | Print list-rate breakdown |
| `--plan-advisor` / `-P` | Compact plan comparison (one Pure API row when scale=1) |
| `--detail` / `-v` | Richer est$ mix + promo table + overage one-liner (FAQ still via `usage info`) |
| `--json` | Machine-readable (`prepaid_balance_usd`, `weekly_usage_pct`, `weekly_resets_at`, …) |

### Plan advisor (`--plan-advisor` / `-P`)

Projects this window’s run-rate to a month and compares:

| Option | Model |
|---|---|
| Pure API (list$) | list rates × tokens, projected |
| Pure API (est$) | list$ × API scale (default 1.0) |
| SuperGrok ~$30 | Weekly pool (small) + **list-rate top-offs** after 100% |
| SuperGrok Heavy ~$300 | Same system, **large** weekly pool; tops are a safety net |

Also prints a **top-off promo scenario** table (full / −25% / free tops by default) so you can model pack discounts without changing the full-price “best fit” winner.

**Caveat:** “Best fit for this window” only holds **if this intensity continues** and assumes **full-price** tops. Promo rows are “if promo holds.” If usage is lower or highly variable, **pure API** is usually safer (no flat Heavy commitment). SuperGrok vs Heavy is read from the billing log (`subscriptionTier`); the planner footer marks your **current plan** and treats the other subscription row as a what-if. Session `/usage` turns do not store the plan. A window that spans an upgrade is labeled as a blend.

Exact weekly pool sizes are **not published** by xAI. Defaults are estimates in code / toml:

```toml
# ~/.grok/grok-utils.toml
[usage]
# Force one scale (optional). Otherwise path + SuperGrok regime pick a default:
# cash_scale = 0.69

cash_scale_api = 1.0                    # API key path → est$ ≈ list$
cash_scale_supergrok_pool = 0.0         # SuperGrok while weekly pool has room
cash_scale_supergrok_overage = 1.9      # SuperGrok Extra Credits (weekly ~100%)
# supergrok_regime = "auto"             # auto | pool | overage

# Top-off pack promo: 0 = full price (normal). Model only — never auto-assumed.
topoff_discount = 0.0                   # 0 | 0.25 | 1.0 (free tops)
topoff_discount_scenarios = [0.20, 0.25, 0.40]  # offered pack promos in -P table
topoff_pack_usd = 100                           # ceil Extra Credit face to pack size

# Plan-advisor subscription knobs
supergrok_usd = 30
heavy_usd = 300
supergrok_weekly_include_usd = 35
heavy_weekly_include_usd = 150
project_days = 30
```

### Maintaining defaults when xAI changes prices (Phase 1)

**v1 does not call the network** from `usage cost`. Defaults live in code (`utils/pricing.py`, including `PRICES_LAST_VERIFIED`) and can be overridden in toml.

When xAI **announces** API list-rate or SuperGrok / Heavy subscription changes:

1. Update built-in rates / plan constants and bump `PRICES_LAST_VERIFIED`, **or**
2. Set the matching keys in `~/.grok/grok-utils.toml` immediately (no release needed)
3. Re-run `--plan-advisor` to see the new break-even

Do **not** rely on silent web scraping for day-to-day reports.

#### Future (not v1): optional list-rate refresh via Models API

xAI exposes model pricing for **API list rates** (not SuperGrok/Heavy subscription fees):

```text
GET https://api.x.ai/v1/models
GET https://api.x.ai/v1/models/{model_id}    # e.g. grok-4.6
Authorization: Bearer <xAI API key>
```

Useful response fields (USD **cents per 100 million tokens**; divide by **100** → **$ per 1M tokens**):

| Field | Meaning |
|---|---|
| `prompt_text_token_price` | Uncached input |
| `cached_prompt_text_token_price` | Cached input |
| `completion_text_token_price` | Output |
| Long-context variants of the above | Higher tier when applicable |

A later opt-in command (e.g. `usage rates-refresh`) could fetch these, cache them, and warn if they diverge from built-in tables. **Subscription** prices ($30 / $300) and **weekly pool** estimates would still need a human check against [x.ai pricing](https://x.ai/pricing) when announced.

## report

**Token path** (list$ primary, path/regime + auth-mix est$, Share(list$), wallet footer) matches **`usage cost`**:

| How you invoke | Path |
|---|---|
| `--by app` (default) | Short names · **always** list$/est$ · **`--tokens` redundant** |
| `--by project` | Full cwd paths; need **`--tokens`** for list$/est$ (else legacy messages) |
| `--by model` / `day` **without** `--tokens` | Legacy session-summary report (no list$/est$) |
| `--by model` / `day` **with** `--tokens` | Token path (list$/est$) |

Plan-advisor (`-P`) stays on `usage cost` only.

```bash
# By app → token path automatically (--tokens optional / no-op)
grok-utils usage report --by app --from 2026-08-01 -m grok-4.6

# By day with list$/est$ (here --tokens matters)
grok-utils usage report --by day --tokens --from 2026-08-01 -m grok-4.6

# Force one uniform est$ scale for the window (optional)
grok-utils usage report --by app --from 2026-08-01 --to 2026-08-05 \
  -m grok-4.6 --cash-scale 0.69
```

## info

```bash
grok-utils usage info
```
