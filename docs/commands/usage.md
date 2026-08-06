# usage

Beautiful usage analytics and **token-based cost estimates** (local sessions only).

```bash
grok-utils usage --help
grok-utils usage info    # FAQ + ledgers (read this if $ numbers confuse you)
```

## FAQ (novice) — Build `/usage` vs grok-utils

Grok Build and this tool show **different meters**. They will **not** match dollar-for-dollar.

| What you see | Where | Plain English |
|---|---|---|
| **Session usage → Cost $28** | Inside Build (`/usage`) | “How expensive was *this chat session* (since start or last resume)?” Build’s own number. |
| **list$ ~$18** | `grok-utils usage cost` | “If the same tokens were billed at **public API list prices**, what would that be?” Offline math. |
| **est$ ~$10** | same table | “Spend-style estimate” = list$ × a scale (default ~0.57) closer to **prepaid credit burn**. |
| **Credits $28** | Build wallet UI | Money **left** in your prepaid balance (account-wide). |
| **Weekly limit 100%** | Build wallet UI | Included weekly pool is **used up**. |
| **Auto topup $20** | Build wallet UI | Account charged **$20** to refill when empty — **not** “this app cost $20.” |

**Example (real shape):** same long coding session can show Session Cost **~$28**, local **list$ ~$18**, **est$ ~$10**, while Credits jump after a **$20** auto top-up and Weekly limit hits **100%**. Tokens in the table may be a few % higher/lower than `/usage` because the UI is a snapshot and local logs keep every completed turn.

**What to use when**

| Goal | Use |
|---|---|
| Which app/day used the most? | `usage cost --by app` or `--by day` |
| Rough real spend pace | **est$** (or re-fit with `--prepaid-usd` + `--credits-remaining`) |
| “How heavy was this session?” | Build **Session Cost** |
| “Am I out of pool / did I top up?” | **Weekly limit** + **Credits** + **Auto topup** |
| API vs SuperGrok vs Heavy? | `usage cost … --plan-advisor` (or `-P`) — only if this intensity **holds** |

```bash
# Typical day-to-day
grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5

# Through latest local data (omit --to)
grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5

# Plan comparison (soft recommendation if run-rate continues)
grok-utils usage cost --from 2026-07-18 --by app -m grok-4.5 -P
```

Same FAQ text is printed by:

```bash
grok-utils usage info
```

## cost (primary)

Estimate from `~/.grok/sessions` turn usage (list rates + spend-oriented scale).

```bash
# Closed window
grok-utils usage cost --from 2026-08-01 --to 2026-08-05 --by app -m grok-4.5

# From a date through latest session data (omit --to; title shows … for open end)
grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5

# Fit est$ to wallet burn for a window (one-shot recalibration)
grok-utils usage cost --from 2026-08-01 --by app \
  --prepaid-usd 60 --credits-remaining 12.12

# Override scale for one run only
grok-utils usage cost ... --cash-scale 0.57

# Plan advisor: pure API vs SuperGrok vs SuperGrok Heavy (run-rate → monthly)
grok-utils usage cost --from 2026-07-18 --by app -m grok-4.5 --plan-advisor
# short flag: -P
```

| Column | Meaning |
|---|---|
| **list$** | Pure API list rates × local tokens (cache included). Transparent upper-bound style. |
| **est$** | **list$ × cash_scale** — spend-oriented, closer to prepaid burn. |

Footer (novice-friendly) plus **effective rates** (list × scale):

```text
list$ = estimated $ at public API prices × your local tokens.
est$  = spend-style estimate (list$ × cash_scale; closer to prepaid burn).
Build Session Cost / Credits / Weekly limit are different meters — they will not match list$/est$.
FAQ: grok-utils usage info   ·   Tune: --cash-scale or config cash_scale
```

### Day-to-day cash scale (config)

Set once so plain `usage cost` needs no scale flags:

```toml
# ~/.grok/grok-utils.toml   (also accepted: ~/.grok/config.toml)
[usage]
cash_scale = 0.57
```

**Cash scale priority:**

1. `--prepaid-usd` + `--credits-remaining` → `scale = (prepaid − remaining) / list$`
2. `--cash-scale N` (this run only)
3. Config `[usage] cash_scale` in `~/.grok/grok-utils.toml` or `config.toml`
4. Built-in default **0.57** (measured API prepaid burn / list$ ratio)

`grok-utils usage cost --help` documents `--cash-scale` and the toml path.  
`grok-utils usage info` prints the full ledger caveats (including the toml example).

### Options

| Flag | Meaning |
|---|---|
| `--from` / `--to` / `--since` | Inclusive dates; omit `--to` for through **latest** session data (`--since` = `--from`). If `--from` is earlier than any turn in the logs, a warning shows the real earliest date (table title uses the data span). |
| `--by` | `app` \| `project` \| `model` \| `day` \| `week` \| `month` |
| `-m` / `--rates-model` | List-rate table for **list$** (default `grok-4.5`) |
| `--cash-scale` | Scale list$ → est$ (else toml / default 0.57) |
| `--prepaid-usd` / `--credits-remaining` | Set scale from wallet burn |
| `--api-estimate` | Print list-rate breakdown |
| `--plan-advisor` / `-P` | Compare pure API vs SuperGrok vs Heavy for this window |
| `--json` | Machine-readable |

### Plan advisor (`--plan-advisor` / `-P`)

Projects this window’s run-rate to a month and compares:

| Option | Model |
|---|---|
| Pure API (list$) | list rates × tokens, projected |
| Pure API (est$) | list$ × cash_scale (prepaid-style) |
| SuperGrok ~$30 | Weekly pool (small) + **list-rate top-offs** after 100% |
| SuperGrok Heavy ~$300 | Same system, **large** weekly pool; tops are a safety net |

**Caveat:** “Best fit for this window” only holds **if this intensity continues**. If usage is lower or highly variable month to month, **pure API (est$)** is usually safer (no flat $300 commitment). Re-run on a quieter window before switching plans.

Exact weekly pool sizes are **not published** by xAI. Defaults are estimates in code / toml:

```toml
# ~/.grok/grok-utils.toml
[usage]
cash_scale = 0.57
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
GET https://api.x.ai/v1/models/{model_id}    # e.g. grok-4.5
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

Token path (`--tokens` or `--by app`) uses the same **list$ + est$** model as `usage cost` (cash_scale default / toml / `--cash-scale`). Plan-advisor stays on `usage cost` only.

```bash
# By app → automatic token path + list$/est$
grok-utils usage report --by app --from 2026-08-01 -m grok-4.5

# Explicit tokens + scale override
grok-utils usage report --tokens --by app --from 2026-08-01 --to 2026-08-05 \
  -m grok-4.5 --cash-scale 0.57
```

## info

```bash
grok-utils usage info
```
