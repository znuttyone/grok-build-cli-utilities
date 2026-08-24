# auth

Offline Grok Build **auth path**: SuperGrok login session vs API key.

```bash
grok-utils auth status
grok-utils auth status --json
grok-utils auth status --history   # change-points from logs/unified.jsonl
```

## Effective method

| Priority | Signal |
|---|---|
| 1 | `[auth] preferred_method = "api_key"` in `~/.grok/config.toml` **and** `XAI_API_KEY` set → **API key** |
| 2 | `~/.grok/auth.json` has session credentials → **SuperGrok session** (wins even if API key is set) |
| 3 | `XAI_API_KEY` only → **API key** |
| 4 | Neither → none |

Matches Build behavior (`cached_token` overrides `xai.api_key` unless preferred method forces API).

## Force API while logged in

```toml
# ~/.grok/config.toml
[auth]
preferred_method = "api_key"
```

Or remove `auth.json` (it can be recreated by `grok login`).

## SuperGrok wallet snapshot

`auth status` also shows (when present in logs):

| Field | Source |
|---|---|
| **Extra Credits $** | `billing: fetched credits config` → `prepaidBalance.val` (cents → USD) |
| **Weekly SuperGrok/Heavy %** | same line → `creditUsagePercent` |
| **Weekly Heavy/SuperGrok pool resets** | same line → `currentPeriod.end` (fallback `billingPeriodEnd`); local time like Build `/usage` “Resets” |

Same offline source as the SuperGrok Usage panel; **not** from turn usage files or a live API. Stale until Build refetches billing. Also printed on `usage cost` footer / JSON (`prepaid_balance_usd`, `weekly_usage_pct`).

## History

`--history` reads process-level auth selection from `~/.grok/logs/unified.jsonl`
(`cached_token` vs `xai.api_key`). Best-effort only — not per-turn billing.
Session files do not store `/session-info` Auth method offline. Pre-log gaps are
labeled **unknown** on cost estimates (list$ scale + caveat).

Also shown on `usage cost` / token `usage report` and as a `doctor` check.
