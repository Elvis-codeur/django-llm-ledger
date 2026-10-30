"""Pricing the way providers bill (issues #1-#4). Expected figures come from the providers' own pricing
pages as read on 2026-10-30, so a test failing here means the ledger disagrees with the provider."""

import uuid
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from llm_ledger.models import LLMCall, ModelPricing, Provider, ProviderPricingPolicy, Run
from llm_ledger.pricing import price_call

pytestmark = pytest.mark.django_db

SATURDAY_NOON = datetime(2026, 10, 24, 12, 0, tzinfo=dt_timezone.utc)  # off-peak for DeepSeek
TUESDAY_2AM = datetime(2026, 10, 27, 2, 0, tzinfo=dt_timezone.utc)  # DeepSeek peak window 01:00-04:00
M = 1_000_000


def _row(model, inp, out, **kw):
    return ModelPricing.objects.create(model=model, input_usd_per_mtok=Decimal(inp), output_usd_per_mtok=Decimal(out),
                                       effective_from=date(2026, 1, 1), **kw)


@pytest.fixture
def deepseek_flash():
    provider = Provider.objects.create(name="deepseek")
    ProviderPricingPolicy.objects.create(provider=provider, offpeak_multiplier=Decimal("0.5"),
                                         peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]},
                                         effective_from=date(2026, 1, 1))
    # api-docs.deepseek.com pricing: peak $0.3 miss / $0.006 hit / $1.2 out; off-peak 50%.
    return _row("deepseek/deepseek-flash", "0.3", "1.2", cache_read_usd_per_mtok=Decimal("0.006"), provider=provider)


class TestCacheReads:
    def test_missing_cache_read_rate_is_flagged_not_silently_zero(self):
        row = _row("deepseek/deepseek-flash", "0.3", "1.2")  # the live row before the fix: no cache-read rate
        priced = price_call(row, at=TUESDAY_2AM, prompt_tokens=M, completion_tokens=M, cache_read_tokens=1490 * M)
        assert priced.total == Decimal("1.500000")  # what CAN be priced
        assert not priced.complete
        assert "cache_read: 1490000000 tokens" in priced.breakdown["unpriced"][0]

    def test_deepseek_cache_hits_at_peak_and_off_peak(self, deepseek_flash):
        peak = price_call(deepseek_flash, at=TUESDAY_2AM, prompt_tokens=M, cache_read_tokens=M, completion_tokens=M)
        assert peak.total == Decimal("1.506000")  # 0.3 + 0.006 + 1.2
        off = price_call(deepseek_flash, at=SATURDAY_NOON, prompt_tokens=M, cache_read_tokens=M, completion_tokens=M)
        assert off.total == Decimal("0.753000")  # page: $0.15 / $0.003 / $0.6 off-peak
        assert off.breakdown["multipliers"] == [["off-peak", "0.5000"]]
        assert off.complete


class TestCacheWrites:
    def test_5_minute_and_1_hour_writes_use_their_own_rates(self):
        # Claude Sonnet 5: $2 input, $2.50 5m write, $4 1h write, $0.20 hit, $10 output.
        row = _row("anthropic/claude-sonnet-5", "2", "10", cache_read_usd_per_mtok=Decimal("0.2"),
                   cache_write_usd_per_mtok=Decimal("2.5"), cache_write_1h_usd_per_mtok=Decimal("4"))
        priced = price_call(row, at=SATURDAY_NOON, cache_creation_tokens=2 * M,
                            usage_extra={"cache_write_1h_tokens": M})
        assert priced.breakdown["parts_usd"] == {"cache_write": "2.500000", "cache_write_1h": "4.000000"}
        assert priced.total == Decimal("6.500000")

    def test_1_hour_writes_without_a_1h_rate_are_flagged(self):
        row = _row("anthropic/x", "2", "10", cache_write_usd_per_mtok=Decimal("2.5"))
        priced = price_call(row, at=SATURDAY_NOON, cache_creation_tokens=M, usage_extra={"cache_write_1h_tokens": M})
        assert not priced.complete and priced.total == Decimal("0")


class TestTiersModifiersUnits:
    def test_long_prompt_tier_counts_cache_tokens_in_the_prompt_length(self):
        # Claude Haiku 5.5: $0.10/$0.50 up to 100k prompt tokens, $0.50/$2.50 above; cache hits count.
        row = _row("anthropic/claude-haiku-5-5", "0.1", "0.5", cache_read_usd_per_mtok=Decimal("0.01"),
                   context_tiers=[{"above_prompt_tokens": 100000, "input_usd_per_mtok": "0.5",
                                   "output_usd_per_mtok": "2.5", "cache_read_usd_per_mtok": "0.05"}])
        short = price_call(row, at=SATURDAY_NOON, prompt_tokens=50_000, completion_tokens=1000)
        assert short.total == Decimal("0.005500") and "tier" not in short.breakdown
        long = price_call(row, at=SATURDAY_NOON, prompt_tokens=50_000, cache_read_tokens=60_000, completion_tokens=1000)
        assert long.breakdown["tier"] == "prompt over 100000 tokens"
        assert long.total == Decimal("0.030500")  # 0.025 + 0.003 + 0.0025

    def test_batch_long_context_matches_openais_published_batch_rates(self):
        # gpt-5.6-luna: long context (>272k) $0.40 / $0.04 / $1.80; Batch long context $0.20 / $0.02 / $0.90.
        row = _row("openai/gpt-5.6-luna", "0.2", "1.2", cache_read_usd_per_mtok=Decimal("0.02"),
                   context_tiers=[{"above_prompt_tokens": 272000, "input_usd_per_mtok": "0.4",
                                   "output_usd_per_mtok": "1.8", "cache_read_usd_per_mtok": "0.04"}],
                   modifier_multipliers={"batch": "0.5", "flex": "0.5", "fast": "2"})
        priced = price_call(row, at=SATURDAY_NOON, prompt_tokens=M, completion_tokens=M, modifiers=["batch"])
        assert priced.total == Decimal("1.100000")  # 0.20 + 0.90

    def test_modifiers_stack(self):
        # Anthropic: batch -50% and inference_geo us x1.1 stack.
        row = _row("anthropic/claude-sonnet-5", "2", "10", modifier_multipliers={"batch": "0.5", "geo:us": "1.1"})
        priced = price_call(row, at=SATURDAY_NOON, prompt_tokens=M, modifiers=["batch", "geo:us"])
        assert priced.total == Decimal("1.100000")

    def test_unit_fees_are_added_at_face_value_not_discounted(self):
        row = _row("anthropic/claude-sonnet-5", "2", "10", modifier_multipliers={"batch": "0.5"},
                   unit_prices={"web_search_requests": {"usd": "10", "per": 1000}})
        priced = price_call(row, at=SATURDAY_NOON, prompt_tokens=M, modifiers=["batch"],
                            usage_extra={"web_search_requests": 3})
        assert priced.breakdown["parts_usd"] == {"input": "1.000000", "web_search_requests": "0.030000"}

    def test_unknown_units_and_modifiers_are_flagged(self):
        row = _row("m", "1", "1")
        priced = price_call(row, at=SATURDAY_NOON, prompt_tokens=M, modifiers=["priority"],
                            usage_extra={"code_execution_hours": 0.5})
        assert not priced.complete and len(priced.breakdown["unpriced"]) == 2

    def test_reasoning_and_image_counts_are_informational_never_double_billed(self):
        row = _row("m", "1", "1")
        priced = price_call(row, at=SATURDAY_NOON, prompt_tokens=M,
                            usage_extra={"reasoning_tokens": 500, "image_input_tokens": 1600, "images": 1})
        assert priced.complete and priced.total == Decimal("1.000000")


class TestApi:
    def _post(self, **body):
        run = Run.objects.create(run_id=uuid.uuid4(), project="p")
        return APIClient().post("/api/calls/", {"run": run.id, "provider": "x", **body}, format="json")

    def test_call_stores_breakdown_and_completeness(self, deepseek_flash):
        resp = self._post(model="deepseek/deepseek-flash", prompt_tokens=M, cache_read_tokens=M,
                          timestamp=SATURDAY_NOON.isoformat(), usage_extra={"web_search_requests": 2})
        assert resp.status_code == 201, resp.data
        assert resp.data["cost_usd"] == "0.153000"  # off-peak: 0.15 input + 0.003 cache hits
        assert resp.data["cost_complete"] is False  # no unit price for searches on this row
        assert resp.data["cost_breakdown"]["parts_usd"]["cache_read"] == "0.003000"

    def test_client_cannot_claim_a_breakdown_or_completeness(self, deepseek_flash):
        resp = self._post(model="deepseek/deepseek-flash", prompt_tokens=M, cost_complete=True,
                          cost_breakdown={"total_usd": "0"}, timestamp=SATURDAY_NOON.isoformat())
        assert resp.data["cost_breakdown"]["total_usd"] == "0.150000"

    def test_subscription_call_gets_a_computed_list_price_equivalent(self, deepseek_flash):
        resp = self._post(model="deepseek/deepseek-flash", prompt_tokens=M, completion_tokens=M, billing="subscription",
                          api_equivalent_cost_usd="99.00", timestamp=TUESDAY_2AM.isoformat())
        assert resp.data["cost_usd"] == "0.000000"
        assert resp.data["api_equivalent_cost_usd"] == "1.500000"  # computed, the client's 99 ignored

    def test_unpriced_model_is_saved_and_marked_incomplete(self):
        resp = self._post(model="deepseek/deepseek-chat", prompt_tokens=100)
        assert resp.status_code == 201 and resp.data["cost_complete"] is False


class TestReprice:
    def test_reprice_after_a_pricing_correction(self):
        wrong = _row("deepseek/deepseek-flash", "0.3", "1.2")
        run = Run.objects.create(run_id=uuid.uuid4(), project="p")
        resp = APIClient().post("/api/calls/", {"run": run.id, "provider": "deepseek", "model": "deepseek/deepseek-flash",
                                                "prompt_tokens": M, "cache_read_tokens": 100 * M,
                                                "timestamp": TUESDAY_2AM.isoformat()}, format="json")
        call = LLMCall.objects.get(pk=resp.data["id"])
        assert call.cost_usd == Decimal("0.3") and not call.cost_complete

        wrong.voided, wrong.void_reason = True, "no cache-read rate"
        wrong.save()
        fixed = _row("deepseek/deepseek-flash", "0.3", "1.2", cache_read_usd_per_mtok=Decimal("0.006"))

        out = StringIO()
        call_command("reprice_calls", "--dry-run", stdout=out)
        assert "would change 1" in out.getvalue() and "$0.3000 -> $0.9000" in out.getvalue()
        call.refresh_from_db()
        assert call.cost_usd == Decimal("0.3")  # dry run wrote nothing

        call_command("reprice_calls", "--model", "deepseek/deepseek-flash", stdout=StringIO())
        call.refresh_from_db()
        assert call.cost_usd == Decimal("0.9") and call.cost_complete and call.pricing_used == fixed
        assert call.cost_breakdown["previous"]["cost_usd"] == "0.300000"
        assert call.cost_breakdown["previous"]["pricing_row"] == wrong.pk

        out = StringIO()
        call_command("reprice_calls", stdout=out)  # idempotent: nothing left to change
        assert "changed 0" in out.getvalue()
