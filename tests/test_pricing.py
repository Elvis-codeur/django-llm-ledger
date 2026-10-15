"""llm_ledger.pricing: historical, time-boxed cost computation -- the core correctness guarantee of
this whole app (a call must be priced at the rate that was ACTUALLY in effect when it happened)."""

from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

import pytest

from llm_ledger.models import ModelPricing
from llm_ledger.pricing import NoPricingError, compute_cost, find_pricing, pricing_covers_date

pytestmark = pytest.mark.django_db


def _price(model="m", **kw):
    defaults = dict(model=model, input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("2"), effective_from=date(2026, 1, 1))
    defaults.update(kw)
    return ModelPricing.objects.create(**defaults)


class TestFindPricing:
    def test_finds_the_only_row(self):
        row = _price()
        assert find_pricing("m", date(2026, 6, 1)).id == row.id

    def test_raises_when_nothing_covers_the_date(self):
        with pytest.raises(NoPricingError):
            find_pricing("unknown-model")

    def test_picks_the_row_whose_window_actually_covers_the_date_not_just_the_latest(self):
        old = _price(model="m", effective_from=date(2026, 1, 1), effective_until=date(2026, 3, 1), input_usd_per_mtok=Decimal("1"))
        new = _price(model="m", effective_from=date(2026, 3, 2), input_usd_per_mtok=Decimal("5"))
        assert find_pricing("m", date(2026, 2, 1)).id == old.id
        assert find_pricing("m", date(2026, 6, 1)).id == new.id

    def test_pricing_covers_date_true_and_false(self):
        _price(model="m", effective_from=date(2026, 1, 1), effective_until=date(2026, 3, 1))
        assert pricing_covers_date("m", date(2026, 2, 1)) is True
        assert pricing_covers_date("m", date(2026, 6, 1)) is False


class TestComputeCost:
    def test_basic_input_and_output_tokens(self):
        pricing = _price(input_usd_per_mtok=Decimal("0.30"), output_usd_per_mtok=Decimal("1.20"))
        at = datetime(2026, 6, 1, 12, 0, tzinfo=dt_timezone.utc)
        cost = compute_cost(pricing, at=at, prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert cost == Decimal("1.500000")

    def test_cache_read_and_write_tokens_included_when_priced(self):
        pricing = _price(
            input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("1"),
            cache_read_usd_per_mtok=Decimal("0.1"), cache_write_usd_per_mtok=Decimal("2"),
        )
        at = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        cost = compute_cost(pricing, at=at, cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
        assert cost == Decimal("2.100000")

    def test_cache_tokens_ignored_when_pricing_has_no_rate_for_them(self):
        pricing = _price(input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("1"))
        at = datetime(2026, 6, 1, tzinfo=dt_timezone.utc)
        cost = compute_cost(pricing, at=at, cache_read_tokens=1_000_000)
        assert cost == Decimal("0")

    def test_offpeak_multiplier_applies_outside_peak_window(self):
        pricing = _price(
            input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("0"),
            offpeak_multiplier=Decimal("0.5"),
            peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]},
        )
        # Monday (weekday 0) at 12:00 UTC -- a weekday, but outside both hour windows -> off-peak.
        at = datetime(2026, 1, 5, 12, 0, tzinfo=dt_timezone.utc)
        assert at.weekday() == 0
        cost = compute_cost(pricing, at=at, prompt_tokens=1_000_000)
        assert cost == Decimal("0.500000")

    def test_offpeak_multiplier_does_not_apply_inside_peak_window(self):
        pricing = _price(
            input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("0"),
            offpeak_multiplier=Decimal("0.5"),
            peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]},
        )
        at = datetime(2026, 1, 5, 2, 0, tzinfo=dt_timezone.utc)  # Monday, 02:00 UTC -- inside [1,4)
        cost = compute_cost(pricing, at=at, prompt_tokens=1_000_000)
        assert cost == Decimal("1.000000")

    def test_offpeak_multiplier_applies_on_a_non_peak_weekday(self):
        pricing = _price(
            input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("0"),
            offpeak_multiplier=Decimal("0.5"),
            peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4]]},
        )
        at = datetime(2026, 1, 3, 2, 0, tzinfo=dt_timezone.utc)  # Saturday
        assert at.weekday() == 5
        cost = compute_cost(pricing, at=at, prompt_tokens=1_000_000)
        assert cost == Decimal("0.500000")
