# django-llm-ledger

A reusable Django app that tracks LLM API cost/usage — runs, calls, and a historical, time-boxed model
pricing table (including provider off-peak/peak pricing) — so you don't have to build this once per
project. Generalized out of a document-translation pipeline that had been tracking real DeepSeek/
Anthropic/Together spend this way in production for months.

Two ways to use it:

## 1. Embedded in your own Django project (fast — same-process DB calls)

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "rest_framework",
    "django_filters",
    "llm_ledger",
]

# urls.py
urlpatterns = [
    ...,
    path("llm-ledger/", include("llm_ledger.urls")),
]
```

Then `python manage.py migrate` and use the models directly (`llm_ledger.models.Run`,
`llm_ledger.models.LLMCall`, `llm_ledger.models.ModelPricing`) or the REST API it exposes.

## 2. Standalone (reachable from other projects over HTTP)

```
pip install django-llm-ledger[standalone]
python -m llm_ledger.standalone.manage migrate
python -m llm_ledger.standalone.manage runserver
```

Defaults to SQLite and `DEBUG=True` for local use — override via `LLM_LEDGER_DB_ENGINE`/
`LLM_LEDGER_DB_NAME`/`LLM_LEDGER_DB_HOST`/etc. environment variables for a real deployment (see
`llm_ledger/standalone/settings.py`). A real deployment should run it behind `gunicorn` (the
`standalone` extra) and a reverse proxy with TLS, same as any other Django service.

## What it tracks

- **`ModelPricing`** — one row per model per time window it was priced at (`effective_from`/
  `effective_until`), with provenance (`source_url`, `verification`, `verified_at`) and optional
  peak/off-peak pricing (`offpeak_multiplier`, `peak_windows_utc`). A price change is a NEW row, never
  an edit to an old one — cost for a call made last month must use the price that was actually in
  effect then.
- **`Run`** — one unit of work worth tracking cost for (a translation pass, a training job, a backfill
  script — whatever your own project calls a "run"). `project` is a free-form string so one ledger can
  serve several unrelated codebases; `label` is likewise free text, since this app has no opinion on
  what stages your pipeline has.
- **`LLMCall`** — one real (or subscription-covered) API call. `cost_usd` is always computed
  **server-side** from `ModelPricing` when you POST raw token counts — a client's claimed cost is
  never trusted. `billing="subscription"` (e.g. a Claude Code / ChatGPT Plus session) is never priced
  (`cost_usd` stays 0, correctly — see the field's docstring); `api_equivalent_cost_usd` can hold an
  estimate of what the same tokens would cost at list price, kept in its own column so it can never
  leak into a real-cost total.
- **`Tag`** — a key/value cost-allocation label (`project:x`, `type:training`), AWS-style, attachable to
  a `Run` or an `Expense` so spend can be sliced by any combination of dimensions later.
- **`Expense`** — any real cost that is NOT a per-token LLM call: GPU pod rental, storage, a flat fee.
  `source` names the billing system (e.g. `"runpod"`), `category` the kind of resource within it.
  Idempotent by `(source, external_id)`, same pattern as `LLMCall.external_id`. Has `ExpenseComment`
  for leaving a note on a weird/unexplained line item. **Append-only**: a wrong `Expense` is voided
  (`voided`/`void_reason`/`superseded_by`), never deleted — see "Accounting notes" below.

## Pricing a call the way providers bill it

`pricing.price_call()` prices every component a provider charges for, and says what it could not price:

- **Tokens**: input, output, cache reads, cache writes. A call's `cache_creation_tokens` is all cache
  writes; send the part written to the 1-hour cache as `usage_extra["cache_write_1h_tokens"]` and it
  is priced at `cache_write_1h_usd_per_mtok` (Anthropic: 2x input, versus 1.25x for 5 minutes).
- **Long prompts**: `ModelPricing.context_tiers`, e.g. OpenAI over 272k input tokens, Claude Haiku 5.5
  over 100k. Prompt length counts input + cache reads + cache writes.
- **Request options**: `LLMCall.pricing_modifiers` (`["batch"]`, `["fast", "geo:us"]`) scaled by
  `ModelPricing.modifier_multipliers`; they stack with each other and with off-peak.
- **Per-unit fees**: `LLMCall.usage_extra` counts (`{"web_search_requests": 3}`) priced by
  `ModelPricing.unit_prices` (`{"web_search_requests": {"usd": "10", "per": 1000}}`), not discounted
  by batch/off-peak.
- **Images and reasoning** are billed by Anthropic, OpenAI and DeepSeek as input and output tokens, so
  they are already inside the token counts. `usage_extra` keys `images`, `image_input_tokens` and
  `reasoning_tokens` are kept for information and never priced a second time.

Every call stores `cost_breakdown` (each part in USD, the tier and multipliers used) and
`cost_complete`. Usage with no rate on the pricing row is **never silently $0**: it is listed in
`cost_breakdown["unpriced"]` and the call gets `cost_complete=False`, so `GET /calls/?cost_complete=false`
shows exactly what pricing data is missing. A subscription call keeps `cost_usd` at 0 and gets the
server-computed list price in `api_equivalent_cost_usd`.

After correcting a price (void the wrong row, add a new one), re-price what was already recorded:

```
python -m llm_ledger.standalone.manage reprice_calls --dry-run          # what would change, per model
python -m llm_ledger.standalone.manage reprice_calls --model deepseek/deepseek-flash
```

Each re-priced call keeps its previous figure under `cost_breakdown["previous"]`.

## API

```
GET/POST   /model-pricing/
GET/POST   /runs/
GET        /runs/<id>/cost/            -- real spend total, subscription calls kept separate
GET/POST   /calls/
GET/POST   /tags/                      -- idempotent by (key, value)
GET        /tags/<id>/cost/            -- LLM + Expense total under this one tag
GET/POST   /expenses/
GET/POST   /expenses/<id>/comments/
POST       /expenses/<id>/void/        -- {"reason": "...", "superseded_by": <id>?}
GET        /cost-summary/?key=...&value=...        (repeatable, ANDs across pairs)
GET        /cost-timeseries/?start=...&end=...&bucket=day|week|month&key=...&value=...
GET        /cost-distribution/?metric=expense|llm_call&start=...&end=...&key=...&value=...
```

A Python SDK for the read/analysis side of this (`cost_summary`, `cost_timeseries`,
`cost_distribution`, tag/expense helpers) lives in the sibling package
[`llm-ledger-client`](https://github.com/Elvis-codeur/llm-ledger-client)'s `LedgerAnalysis` class.

## Accounting notes

This isn't built by an accountant, but it follows a few real bookkeeping conventions on purpose:

- **Accrual, not cash, basis.** `occurred_at`/`timestamp` record when a cost was actually incurred
  (when the GPU ran, when the API call happened), not when a card was charged or an invoice was paid.
  That's the basis that actually lines up with which project/experiment caused the spend.
- **Append-only — never delete a transaction.** A wrong `Expense` gets voided (kept, with
  `void_reason`, optionally linked via `superseded_by` to its correction), the same way real
  bookkeeping reverses an entry instead of erasing it. All cost-reading endpoints exclude voided rows
  by default, but the row — and the record of why it was once believed correct — never disappears.
- **Keep the source document.** `Expense.raw` stores the original billing record a cost was imported
  from, the same instinct as keeping a receipt — never trusted over `amount_usd` for the actual sum,
  but there for when someone needs to see where a number came from.
- **A unique reference number per transaction.** `external_id` (unique per `source`/`run`) is the same
  idea as a unique invoice or transaction ID — it's what makes re-running an importer safe.
- **Reconcile against the provider's own totals periodically.** This ledger doesn't do this
  automatically; if you import per-resource detail (e.g. per-pod RunPod billing), periodically compare
  the sum against the provider's own aggregate billing total for the same window as a sanity check.

Every viewset declares its filter fields explicitly — an undeclared query param is ignored by DRF, not
an error, so don't expect an unlisted filter to do anything; check `views.py` for what's declared.

## Why "never a client-claimed cost"

A real incident in the project this was generalized from: pricing drifts, provider aliases get
repointed, and a client-side bug can silently misreport spend for months before anyone notices. Posting
raw token counts and letting the ledger itself look up the price in effect at that *timestamp* is the
only way to get a cost figure you can actually trust months later.

## Status

Early, freshly extracted (2026-10-15). Tests: `pytest` (32 passing). Not yet used in production outside
the project it came from — treat 0.x as "working, not yet battle-tested as a standalone package." The
Tag/Expense/analytics surface was live-tested against real RunPod billing data the same day it was
added.
