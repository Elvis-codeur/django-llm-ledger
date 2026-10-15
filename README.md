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

## API

```
GET/POST   /model-pricing/
GET/POST   /runs/
GET        /runs/<id>/cost/        -- real spend total, subscription calls kept separate
GET/POST   /calls/
```

Every viewset declares its filter fields explicitly — an undeclared query param is ignored by DRF, not
an error, so don't expect an unlisted filter to do anything; check `views.py` for what's declared.

## Why "never a client-claimed cost"

A real incident in the project this was generalized from: pricing drifts, provider aliases get
repointed, and a client-side bug can silently misreport spend for months before anyone notices. Posting
raw token counts and letting the ledger itself look up the price in effect at that *timestamp* is the
only way to get a cost figure you can actually trust months later.

## Status

Early, freshly extracted (2026-10-15). Tests: `pytest` (16 passing). Not yet used in production outside
the project it came from — treat 0.x as "working, not yet battle-tested as a standalone package."
