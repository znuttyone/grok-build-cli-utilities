"""Usage cost FAQ / caveats text (kept out of pricing.py)."""

# Plain multi-line footer (print with markup=False — [brackets] are not Rich tags).
COST_CAVEATS_SHORT = """\
list$ = estimated $ at public API prices × your local tokens.
est$  = path/regime spend lens (API ≈ list$; SuperGrok pool ≈ 0; overage ≈ 1.9× list$).
Extra Credits $ and weekly % come from billing lines in logs/unified.jsonl (also auth status).
Build Session Cost / Credits / Weekly limit are different meters — they will not match list$/est$.
FAQ: grok-utils usage info   ·   Tune: path scales or --cash-scale / --topoff-discount"""

COST_CAVEATS_LONG = """
FAQ — Why numbers do not match Grok Build /usage
------------------------------------------------
Grok Build and grok-utils show different *kinds* of money and tokens. None is
"wrong"; they answer different questions.

  Q: What is list$?
  A: Offline "as if API list rates" dollar total for the filtered turns:
       list$ = tokens × published rates for --rates-model (default grok-4.5).
     Data source: turn usage under ~/.grok/sessions (not Build Session Cost,
     not the SuperGrok wallet, not X console).
     Cache% in the table (and why it matters for list$):
       Build reuses prompt/context tokens already paid for → those count as
       *cached* input, billed at the lower cached rate (e.g. $0.30/1M vs
       $2.00/1M uncached on grok-4.5). High cache% usually means *lower* list$
       for the same total tokens — that is expected efficiency, not a bug.
     Formula sketch:
       list$ = cached/1e6×cached_rate + uncached_in/1e6×input_rate
             + (output+reasoning)/1e6×output_rate

  Q: What is est$?
  A: Best-effort *spend lens* on top of list$ (not a second token meter):
       est$ = list$ × path/regime scale.
       API path              → scale 1.0 (est$ ≈ list$)
       SuperGrok pool        → weekly limit < ~100% → Extra Credits idle → ~0
       SuperGrok overage     → weekly ~100% → Extra Credits burn → ~1.9× list$
       weekly % unknown      → scale 1.0 + caveat (do not invent pool/overage)
     Optional override: --cash-scale or [usage] cash_scale (legacy single blend).

  Q: Why does Build Session Cost ($28) differ from list$ (~$18) on the same work?
  A: Session Cost is Build's own meter for *this session* (since start or last
     resume). list$ is public API rates × local tokens. Different accounting;
     Session Cost is NOT used in list$/est$. Tokens can also differ slightly
     (UI snapshot vs full local log of completed turns).

  Q: What are Credits, Weekly limit 100%, Auto topup $20 — do they change list$/est$?
  A: Those are *account-wide SuperGrok wallet* meters (Build UI), not per-app:
       Extra Credits / Credits = prepaid balance still available
       Weekly limit            = included SuperGrok pool (100% = exhausted)
       Auto topup              = e.g. $20 face charged to *refill* Extra Credits
                                 (not "this app cost $20")
     How they affect grok-utils output:
       • list$  — NOT affected. Always tokens × API list rates.
       • Credits $ / Auto topup amount — NOT plugged into the cost formula.
         Shown only as a *snapshot* (footer / auth status) so you can see what
         the wallet looks like. We do not auto-subtract top-ups from list$/est$.
       • Weekly % — only switch for SuperGrok *est$* regime (pool ≈ 0 vs
         overage ≈ 1.9×). It does not change list$ or the Share bars.
     Offline snapshot source: logs/unified.jsonl billing lines
     (prepaidBalance, creditUsagePercent) via auth status / cost footer.

  Q: What does "Wallet / auth  Extra Credits $… · weekly …% · SuperGrok session"
     on usage cost / report mean?
  A: One-line *account snapshot for this machine right now* — not per app and
     not the table math:
       Extra Credits $  — prepaid SuperGrok balance left (billing log)
       weekly N%        — SuperGrok weekly pool used so far this period
                          (only switches SuperGrok *est$* pool vs overage;
                          does not change list$ or Share bars)
       SuperGrok / API  — which auth path Build prefers on this machine now
     Does not subtract Credits from list$/est$. Does not mean "this app spent
     $21.40". Same fields as: grok-utils auth status
     (source: logs/unified.jsonl billing fetch, best-effort / can be stale).

  Q: SuperGrok session vs API key — why did the X console miss my
     account / Extra Credits burn?
  A: Two different billing systems. If ~/.grok/auth.json has a SuperGrok
     login session, Build prefers that path (cached_token) even when
     XAI_API_KEY is set. Token use and auto top-ups then hit the SuperGrok
     wallet (weekly pool + Extra Credits), not the API-key prepaid/paygo
     ledger shown under X console "API key Usage". So the console can look
     quiet while SuperGrok Credits still move. Check: grok-utils auth status
     Force API: [auth] preferred_method = "api_key" in config.toml, or remove
     auth.json (recreated on grok login). Auth history is best-effort process-
     level timeline from logs — not per-turn billing. Pre-log gaps are labeled
     unknown.  grok-utils auth status --history

  Q: Top-off promo −25% or free (−100%) credits?
  A: Default assumes full pack price (topoff_discount = 0). Model promos:
       --topoff-discount 0.25   or  1.0 (free tops)
       [usage] topoff_discount = 0.25
     Plan-advisor always shows card scenarios (full / −25% / free) without
     changing the full-price "best fit" winner. Free tops → card overage $0;
     you still pay the SuperGrok/Heavy subscription.

  Q: Which number should I trust for budgeting?
  A: - Per-app / per-day activity → list$
     - Extra-credit burn estimate → est$ (regime-aware)
     - "How expensive was this chat session?" → Build Session Cost
     - "How much is left / weekly pool?" → Extra Credits $ + weekly % (auth status)
     - "Which plan if I keep this pace?" → usage cost --plan-advisor (-P)

  Q: Common commands (novice)
  A:  grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5
      grok-utils usage cost --from 2026-08-01 --by app -m grok-4.5 -P
      grok-utils usage report --by app --from 2026-08-01
      # --tokens is only needed when not grouping by app, e.g.:
      # grok-utils usage report --by day --tokens --from 2026-08-01
      grok-utils auth status
      grok-utils usage cost ... -P --topoff-discount 0.25

CAVEATS & COST LEDGERS (technical)
----------------------------------
Where list$/tokens come from (local only — no network):
  Each completed Build turn writes usage into that session's updates.jsonl
  under ~/.grok/sessions/...  Event: turn_completed.usage.
  Why this matters: grok-utils never sees your live X console or SuperGrok
  card charges; it reconstructs activity from these on-disk turn records.

  Fields we use (what / why):
    inputTokens       — total prompt/context tokens for the turn (basis for
                        input $; split into cached vs uncached below)
    cachedReadTokens  — portion of input served from cache (cheaper list rate;
                        drives Cache% and lowers list$ for the same size turns)
    outputTokens      — model reply tokens (higher list rate than input)
    reasoningTokens   — "thinking" tokens when the model emits them; we price
                        them like output (same $/1M) because list tables do
    costUsdTicks      — Build's internal tick counter (optional --list-price /
                        invoice allocation only; NOT used for list$/est$)
    modelCalls        — how many model invocations the turn made (diagnostics)

  Dedup by prompt_id: if the same prompt is logged more than once (retries,
  partial writes), we keep the row with the largest totalTokens so one user
  message is not double-counted in list$/est$.

list$ formula (always prices cache at the cached rate):
  cached/1e6 × cached_rate + uncached_in/1e6 × input_rate
  + (output+reasoning)/1e6 × output_rate
  where uncached_in ≈ max(0, input − cached)

est$ = list$ × cash_scale (path/regime or forced)
  Built-in path defaults (unless forced):
    cash_scale_api = 1.0
    cash_scale_supergrok_pool = 0.0
    cash_scale_supergrok_overage = 1.9
  Force one number for a window:
    --cash-scale N
    --prepaid-usd + --credits-remaining  (scale = wallet burn / list$)
    [usage] cash_scale = 0.69   # optional legacy blend; disables path split

  Top-off promo (card, not list$):
    --topoff-discount 0..1   or  [usage] topoff_discount
    est_cash$ = est$ × (1 − discount) when discount > 0
    plan-advisor scenarios: topoff_discount_scenarios = [0.0, 0.25, 1.0]

Effective rates (list × scale) assume a *uniform* scale vs list when forced.
  Prepaid may not discount cached/input/output equally — only wallet total is known offline.

--plan-advisor / -P
  Compare pure API vs SuperGrok vs SuperGrok Heavy for the same window
  (run-rate → monthly projection). "Best fit" assumes this window's intensity
  continues and full-price tops. Promo rows (−25%, free tops, …) are "if promo
  holds" only. Weekly pool $ sizes are estimates. Heavy is not auto-detected
  from auth (same SuperGrok session path offline).

  [usage]
  supergrok_usd = 30
  heavy_usd = 300
  supergrok_weekly_include_usd = 35
  heavy_weekly_include_usd = 150
  project_days = 30
  topoff_discount = 0.0
  topoff_discount_scenarios = [0.0, 0.25, 1.0]

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
