"""Provider: a billing-strategy identity independent of any one model, and the peak/off-peak fallback
from ModelPricing onto its Provider (2026-10-15 addition)."""

import uuid
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from llm_ledger.models import LLMCall, ModelPricing, Provider, Run
from llm_ledger.pricing import compute_cost, provider_is_peak

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


def test_provider_with_no_peak_schedule_has_no_peak_concept(client):
    provider = Provider.objects.create(name="flatpay")
    resp = client.get(f"/api/providers/{provider.id}/peak_status/")
    assert resp.status_code == 200, resp.data
    assert resp.data["has_peak_schedule"] is False
    assert resp.data["is_peak"] is None


def test_provider_peak_status_at_a_given_time(client):
    # DeepSeek-style: peak Mon-Fri 06:00-18:00 UTC, off-peak outside that.
    provider = Provider.objects.create(
        name="deepseek", offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
    )
    peak_time = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-12T10:00:00Z"})  # Monday 10:00
    assert peak_time.data["is_peak"] is True

    offpeak_time = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-12T22:00:00Z"})  # Monday 22:00
    assert offpeak_time.data["is_peak"] is False

    weekend = client.get(f"/api/providers/{provider.id}/peak_status/", {"at": "2026-10-17T10:00:00Z"})  # Saturday
    assert weekend.data["is_peak"] is False


def test_modelpricing_falls_back_to_its_providers_peak_schedule(client):
    """The real point of this feature: set the peak schedule ONCE on the Provider, and every
    ModelPricing row for it inherits it without repeating the windows per model."""
    provider = Provider.objects.create(
        name="deepseek", offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
    )
    pricing = ModelPricing.objects.create(
        model="deepseek/deepseek-flash", provider=provider,
        input_usd_per_mtok=Decimal("1.00"), output_usd_per_mtok=Decimal("1.00"),
        effective_from=date(2026, 1, 1),
        # NOTE: no offpeak_multiplier/peak_windows_utc set on the row itself.
    )
    run = Run.objects.create(run_id=uuid.uuid4(), project="p")

    peak_call = LLMCall.objects.create(
        run=run, provider="deepseek", model="deepseek/deepseek-flash", billing="api",
        prompt_tokens=1_000_000, pricing_used=pricing,
    )
    peak_cost = compute_cost(pricing, at=datetime(2026, 10, 12, 10, 0, tzinfo=dt_timezone.utc), prompt_tokens=1_000_000)
    offpeak_cost = compute_cost(pricing, at=datetime(2026, 10, 12, 22, 0, tzinfo=dt_timezone.utc), prompt_tokens=1_000_000)

    assert peak_cost == Decimal("1.000000")
    assert offpeak_cost == Decimal("0.500000")  # provider's 0.5x multiplier applied via fallback


def test_a_models_own_offpeak_override_wins_over_its_providers(client):
    provider = Provider.objects.create(
        name="deepseek", offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
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


def test_provider_is_peak_helper_matches_the_view(client):
    provider = Provider.objects.create(
        name="deepseek", offpeak_multiplier=Decimal("0.5"),
        peak_windows_utc={"weekdays": [0, 1, 2, 3, 4], "hours": [[6, 18]]},
    )
    assert provider_is_peak(provider, datetime(2026, 10, 12, 10, 0, tzinfo=dt_timezone.utc)) is True
    assert provider_is_peak(provider, datetime(2026, 10, 12, 22, 0, tzinfo=dt_timezone.utc)) is False
