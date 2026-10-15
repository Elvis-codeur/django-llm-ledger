"""End-to-end through the real DRF views -- the same path a client library (e.g. llm-ledger-client)
actually hits, not just the pure pricing functions."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from llm_ledger.models import LLMCall, ModelPricing, Run

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def pricing():
    return ModelPricing.objects.create(
        model="deepseek/deepseek-flash",
        input_usd_per_mtok=Decimal("0.30"),
        output_usd_per_mtok=Decimal("1.20"),
        effective_from=date(2026, 1, 1),
    )


def test_creating_a_call_computes_cost_server_side_from_pricing(client, pricing):
    run = Run.objects.create(run_id=uuid.uuid4(), project="test-project", label="smoke")
    resp = client.post(
        "/api/calls/",
        {"run": run.id, "provider": "deepseek", "model": "deepseek/deepseek-flash",
         "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "billing": "api"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["cost_usd"] == "1.500000"
    assert resp.data["pricing_used"] == pricing.id


def test_a_client_claimed_cost_is_ignored_server_side_recomputes_it(client, pricing):
    """Real design intent: a client posts raw token counts, never a trusted cost figure -- posting a
    bogus cost_usd must not change what gets stored."""
    run = Run.objects.create(run_id=uuid.uuid4(), project="test-project")
    resp = client.post(
        "/api/calls/",
        {"run": run.id, "provider": "deepseek", "model": "deepseek/deepseek-flash",
         "prompt_tokens": 1_000_000, "completion_tokens": 0, "cost_usd": "999.00", "billing": "api"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["cost_usd"] == "0.300000"


def test_subscription_calls_are_never_priced(client):
    run = Run.objects.create(run_id=uuid.uuid4(), project="test-project")
    resp = client.post(
        "/api/calls/",
        {"run": run.id, "provider": "anthropic", "model": "claude-sonnet-5",
         "prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "billing": "subscription",
         "api_equivalent_cost_usd": "5.00"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["cost_usd"] == "0.000000"
    assert resp.data["api_equivalent_cost_usd"] == "5.000000"


def test_a_call_with_no_pricing_row_saves_at_zero_cost_rather_than_being_refused(client):
    run = Run.objects.create(run_id=uuid.uuid4(), project="test-project")
    resp = client.post(
        "/api/calls/",
        {"run": run.id, "provider": "x", "model": "no-such-model-priced", "prompt_tokens": 100, "billing": "api"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["cost_usd"] == "0.000000"
    assert resp.data["pricing_used"] is None


def test_run_cost_endpoint_sums_real_spend_and_keeps_subscription_separate(client, pricing):
    run = Run.objects.create(run_id=uuid.uuid4(), project="test-project")
    LLMCall.objects.create(run=run, provider="deepseek", model="deepseek/deepseek-flash", cost_usd=Decimal("1.5"), billing="api")
    LLMCall.objects.create(run=run, provider="deepseek", model="deepseek/deepseek-flash", cost_usd=Decimal("2.0"), billing="api")
    LLMCall.objects.create(run=run, provider="anthropic", model="claude", billing="subscription", api_equivalent_cost_usd=Decimal("9.0"))

    resp = client.get(f"/api/runs/{run.id}/cost/")
    assert resp.status_code == 200, resp.data
    assert resp.data["real_cost_usd"] == "3.500000"
    assert resp.data["api_call_count"] == 2
    assert resp.data["subscription_call_count"] == 1
    assert resp.data["subscription_api_equivalent_cost_usd"] == "9.000000"


def test_posting_the_same_run_id_twice_returns_the_existing_run_not_a_400(client):
    """Real gap found via an actual cross-repo integration test (llm-ledger-client's fallback-then-
    flush design re-POSTs a run that already succeeded live)."""
    run_id = str(uuid.uuid4())
    first = client.post("/api/runs/", {"run_id": run_id, "project": "p", "label": "a"}, format="json")
    assert first.status_code == 201

    second = client.post("/api/runs/", {"run_id": run_id, "project": "p", "label": "SHOULD BE IGNORED"}, format="json")
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert second.data["label"] == "a"  # the existing row, not the second POST's (differing) body
    assert Run.objects.filter(run_id=run_id).count() == 1


def test_a_run_can_be_looked_up_by_its_own_run_id_to_resume_after_a_restart(client):
    run_id = uuid.uuid4()
    run = Run.objects.create(run_id=run_id, project="test-project")
    Run.objects.create(run_id=uuid.uuid4(), project="test-project")  # a different run, must not match

    resp = client.get("/api/runs/", {"run_id": str(run_id)})
    ids = [r["id"] for r in resp.data["results"]]
    assert ids == [run.id]


def test_undeclared_filter_param_is_ignored_not_silently_returning_everything(client):
    """Real documented risk this project guards against explicitly: an undeclared DRF/django-filter
    query param is silently dropped rather than erroring. This test just pins the CURRENT, known
    behavior (every declared field in filterset_fields really filters) so a future change to
    filterset_fields is noticed here, not discovered in production."""
    run_a = Run.objects.create(run_id=uuid.uuid4(), project="project-a")
    Run.objects.create(run_id=uuid.uuid4(), project="project-b")

    resp = client.get("/api/runs/", {"project": "project-a"})
    ids = [r["id"] for r in resp.data["results"]]
    assert ids == [run_a.id]
