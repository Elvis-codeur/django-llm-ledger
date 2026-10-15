"""Shared aggregation helpers for the read-side analytics endpoints (cost-timeseries,
cost-distribution) -- pulled out of views.py so the same "filter by tag pairs", "bucket by day/week/
month", and "compute distribution stats" logic isn't duplicated per endpoint, and so an agent calling
this API never has to reimplement any of it client-side (the whole point of adding these endpoints)."""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import QuerySet
from django.utils.dateparse import parse_date as _parse_date
from django.utils.dateparse import parse_datetime as _parse_datetime
from django.utils.timezone import is_naive, make_aware

VALID_BUCKETS = {"day", "week", "month"}


class BadQueryParam(ValueError):
    """Raised for a malformed query param -- callers turn this into a 400, not a 500."""


def parse_query_datetime(value: str, *, end_of_day: bool = False) -> datetime:
    """Accepts either a plain date ("2026-10-10") or a full ISO datetime. A plain end date is treated
    as end-of-that-day (23:59:59.999999) when `end_of_day=True`, so `?end=2026-10-10` includes
    everything that happened ON the 10th, not just up to midnight at its start -- the real off-by-one a
    naive "< end" comparison would otherwise produce for the single most common way to pass an end
    date.

    `parse_date` is tried FIRST, deliberately: Django's own `parse_datetime` silently accepts a bare
    date string too (returning midnight) rather than rejecting it, which would otherwise always skip
    the end-of-day adjustment below -- a real bug caught by a test asserting an `?end=<date>` filter
    actually includes that whole day."""
    d = _parse_date(value)
    if d is not None:
        dt = datetime.combine(d, datetime.max.time() if end_of_day else datetime.min.time())
    else:
        dt = _parse_datetime(value)
        if dt is None:
            raise BadQueryParam(f"'{value}' is not a valid ISO date or datetime")
    if is_naive(dt):
        dt = make_aware(dt)
    return dt


def filter_by_tag_pairs(queryset: QuerySet, pairs: list[tuple[str, str]]) -> QuerySet:
    """ANDs across distinct tag (key, value) pairs on any queryset with a `tags` M2M -- each pair needs
    its own `.filter()` call (a separate join), since one joined row can't simultaneously match two
    different tags, so combining them in a single filter()/Q() would always return nothing for more
    than one pair. Works for Run or Expense (whichever queryset is passed)."""
    for key, value in pairs:
        queryset = queryset.filter(tags__key=key, tags__value=value)
    return queryset.distinct() if pairs else queryset


def bucketed_sums(queryset: QuerySet, date_field: str, amount_field: str, bucket: str) -> dict[date, Decimal]:
    """{bucket_start_date: total_amount} for one queryset, e.g. LLMCall costs or Expense amounts,
    truncated to day/week/month on `date_field`. Summed in Python over the queryset's own rows rather
    than a DB-side Sum() annotation -- a cost ledger has few enough rows in practice that this is
    simpler and stays backend-agnostic (sqlite vs postgres) without a second helper per backend."""
    if bucket not in VALID_BUCKETS:
        raise BadQueryParam(f"bucket must be one of {sorted(VALID_BUCKETS)}, got '{bucket}'")
    totals: dict[date, Decimal] = {}
    for obj in queryset:
        b = trunc_date(getattr(obj, date_field), bucket)
        totals[b] = totals.get(b, Decimal(0)) + Decimal(getattr(obj, amount_field))
    return totals


def trunc_date(dt: datetime, bucket: str) -> date:
    d = dt.date() if hasattr(dt, "date") else dt
    if bucket == "day":
        return d
    if bucket == "week":
        return d - timedelta(days=d.weekday())
    if bucket == "month":
        return d.replace(day=1)
    raise BadQueryParam(f"bucket must be one of {sorted(VALID_BUCKETS)}, got '{bucket}'")


def compute_distribution(values: list[Decimal]) -> dict:
    """count/sum/mean/median/stdev/min/max/percentiles over a list of amounts -- the actual "what's the
    probability distribution of my individual run costs" answer, computed once here so no caller has
    to pull raw rows and do this itself."""
    if not values:
        return {
            "count": 0, "sum_usd": "0", "mean_usd": None, "median_usd": None, "stdev_usd": None,
            "min_usd": None, "max_usd": None, "percentiles_usd": {},
        }
    floats = [float(v) for v in values]
    sorted_floats = sorted(floats)

    def percentile(p: float) -> float:
        if len(sorted_floats) == 1:
            return sorted_floats[0]
        k = (len(sorted_floats) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(sorted_floats) - 1)
        frac = k - lo
        return sorted_floats[lo] + (sorted_floats[hi] - sorted_floats[lo]) * frac

    return {
        "count": len(values),
        "sum_usd": str(sum(values)),
        "mean_usd": str(statistics.mean(floats)),
        "median_usd": str(statistics.median(floats)),
        "stdev_usd": str(statistics.stdev(floats)) if len(floats) > 1 else "0",
        "min_usd": str(min(values)),
        "max_usd": str(max(values)),
        "percentiles_usd": {
            "p10": str(percentile(0.10)), "p25": str(percentile(0.25)), "p50": str(percentile(0.50)),
            "p75": str(percentile(0.75)), "p90": str(percentile(0.90)), "p95": str(percentile(0.95)),
            "p99": str(percentile(0.99)),
        },
    }
