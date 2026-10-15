"""Provider: a billing-strategy identity independent of any one model, with its peak/off-peak POLICY
time-boxed in ProviderPricingPolicy -- a real gap the user caught live, 2026-10-15, before a single
mutable Provider.offpeak_multiplier/peak_windows_utc could ever drift: a provider changing its peak-hour
policy must never retroactively change how a past call's cost was computed."""

import uuid
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from llm_ledger.models import LLMCall, ModelPricing, Provider, ProviderPricingPolicy, Run
from llm_ledger.pricing import compute_cost, find_provider_policy, provider_is_peak

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def test_posting_the_same_provider_name_twice_returns_the_existing_one(client):
    first = client.post("/api/providers/", {"name": "deepseek"}, format="json")
    assert first.status_code == 201

    second = client.post("/api/providers/", {"name": "deepseek", "display_name": "SHOULD BE IGNORED"}, format="json")
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert Provider.objects.count() == 1


def test_provider_with_no_policy_at_all_has_no_peak_concept(client):
    provider = Provider.objects.create(name="flatpay")
    resp = client.get(f"/api/providers/{provider.id}/peak_status/")
    assert resp.status_code == 200, resp.data
    assert resp.data["has_peak_schedule"] is False
    assert resp.data["is_peak"] is None


def test_provider_peak_status_at_a_given_time(client):
    provider = Provider.objects.create(name="deepseek")
    ProviderPricingPolicy.objects.create(
        provider=provider, offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
        effective_from=date(2026, 1, 1),
    )
    peak_time = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-12T10:00:00Z"})  # Monday 10:00
    assert peak_time.data["is_peak"] is True

    offpeak_time = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-12T22:00:00Z"})  # Monday 22:00
    assert offpeak_time.data["is_peak"] is False

    weekend = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-17T10:00:00Z"})  # Saturday
    assert weekend.data["is_peak"] is False


def test_a_changed_policy_does_not_retroactively_change_a_past_call(client):
    """The exact scenario the user flagged: a provider's peak-hour policy changes over time. The OLD
    policy must still govern a call made while it was in effect; the NEW policy must never be applied
    to that past call just because it's "current" now."""
    provider = Provider.objects.create(name="deepseek")
    old_policy = ProviderPricingPolicy.objects.create(
        provider=provider, offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
        effective_from=date(2026, 1, 1), effective_until=date(2026, 6, 30),
    )
    new_policy = ProviderPricingPolicy.objects.create(
        provider=provider, offpeak_multiplier=Decimal("0.8"),  # the provider made off-peak less generous
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[0, 24]]},  # and "peak" now covers the whole day
        effective_from=date(2026, 7, 1),
    )

    # A call made back in March, under the OLD policy -- 22:00 was off-peak under the old hours.
    march_time = datetime(2026, 3, 10, 22, 0, tzinfo=dt_timezone.utc)
    assert find_provider_policy(provider, march_time).id == old_policy.id
    assert provider_is_peak(provider, march_time) is False  # old policy: 22:00 is outside [6,18) -> off-peak

    # The SAME clock time, same weekday (Monday), under the NEW policy -- peak all day now.
    new_policy_time = datetime(2026, 10, 12, 22, 0, tzinfo=dt_timezone.utc)  # a Monday
    assert find_provider_policy(provider, new_policy_time).id == new_policy.id
    assert provider_is_peak(provider, new_policy_time) is True  # new policy: peak covers [0,24)


def test_modelpricing_falls_back_to_the_providers_policy_in_effect_at_the_calls_own_timestamp(client):
    """The real point of this feature: set the peak schedule on the Provider's policy, and every
    ModelPricing row for it inherits whichever policy applied AT THE CALL'S OWN TIME -- not today's."""
    provider = Provider.objects.create(name="deepseek")
    ProviderPricingPolicy.objects.create(
        provider=provider, offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
        effective_from=date(2026, 1, 1),
    )
    pricing = ModelPricing.objects.create(
        model="deepseek/deepseek-flash", provider=provider,
        input_usd_per_mtok=Decimal("1.00"), output_usd_per_mtok=Decimal("1.00"),
        effective_from=date(2026, 1, 1),
        # NOTE: no offpeak_multiplier/peak_windows_utc set on the row itself.
    )
    run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    LLMCall.objects.create(
        run=run, provider="deepseek", model="deepseek/deepseek-flash", billing="api",
        prompt_tokens=1_000_000, pricing_used=pricing,
    )

    peak_cost = compute_cost(pricing, at=datetime(2026, 10, 12, 10, 0, tzinfo=dt_timezone.utc), prompt_tokens=1_000_000)
    offpeak_cost = compute_cost(pricing, at=datetime(2026, 10, 12, 22, 0, tzinfo=dt_timezone.utc), prompt_tokens=1_000_000)

    assert peak_cost == Decimal("1.000000")
    assert offpeak_cost == Decimal("0.500000")  # provider's 0.5x multiplier applied via fallback


def test_a_models_own_offpeak_override_wins_over_its_providers_policy(client):
    provider = Provider.objects.create(name="deepseek")
    ProviderPricingPolicy.objects.create(
        provider=provider, offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
        effective_from=date(2026, 1, 1),
    )
    pricing = ModelPricing.objects.create(
        model="deepseek/special-model", provider=provider,
        input_usd_per_mtok=Decimal("1.00"), output_usd_per_mtok=Decimal("1.00"),
        effective_from=date(2026, 1, 1),
        offpeak_multiplier=Decimal("0.9"),  # this model's own override, different from the provider's
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
    )
    offpeak_cost = compute_cost(pricing, at=datetime(2026, 10, 12, 22, 0, tzinfo=dt_timezone.utc), prompt_tokens=1_000_000)
    assert offpeak_cost == Decimal("0.900000")  # the model's own 0.9x, not the provider's 0.5x


def test_creating_a_provider_pricing_policy_via_the_api(client):
    provider = Provider.objects.create(name="deepseek")
    resp = client.post(
        "/api/provider-pricing-policies/",
        {
            "provider": provider.id, "offpeak_multiplier": "0.5",
            "peak_windows_utc": {"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]},
            "effective_from": "2026-01-01", "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "verification": "primary",
        },
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert ProviderPricingPolicy.objects.filter(provider=provider).count() == 1
