"""Tags + Expense + cross-cutting cost-summary endpoint: the non-LLM-cost / cost-attribution extension
added 2026-10-15 so a GPU rental (RunPod, etc.) or any other flat-fee cost can be tracked and attributed
to the same projects/experiments as LLM calls."""

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from llm_ledger.models import Expense, LLMCall, Run, Tag

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def test_posting_the_same_tag_twice_returns_the_existing_one_not_a_400(client):
    first = client.post("/api/tags/", {"key": "project", "value": "agentic-reasoning-translator"}, format="json")
    assert first.status_code == 201

    second = client.post("/api/tags/", {"key": "project", "value": "agentic-reasoning-translator"}, format="json")
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert Tag.objects.count() == 1


def test_a_run_can_be_tagged_and_filtered_by_tag(client):
    tag = Tag.objects.create(key="project", value="reasoner")
    run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    run.tags.add(tag)
    Run.objects.create(run_id=uuid.uuid4(), project="p")  # untagged, must not match

    resp = client.get("/api/runs/", {"tags": tag.id})
    ids = [r["id"] for r in resp.data["results"]]
    assert ids == [run.id]


def test_posting_the_same_expense_external_id_twice_returns_the_existing_one(client):
    first = client.post(
        "/api/expenses/",
        {"source": "runpod", "category": "gpu-pod", "amount_usd": "1.2345", "external_id": "runpod-bill-1"},
        format="json",
    )
    assert first.status_code == 201

    second = client.post(
        "/api/expenses/",
        {"source": "runpod", "category": "gpu-pod", "amount_usd": "999.00", "external_id": "runpod-bill-1"},
        format="json",
    )
    assert second.status_code == 200
    assert second.data["id"] == first.data["id"]
    assert second.data["amount_usd"] == "1.234500"  # the existing row, not the (differing) re-post
    assert Expense.objects.count() == 1


def test_expense_can_be_created_with_tags(client):
    tag = Tag.objects.create(key="project", value="reasoner")
    resp = client.post(
        "/api/expenses/",
        {"source": "runpod", "category": "gpu-pod", "amount_usd": "3.50", "tags": [tag.id]},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    expense = Expense.objects.get(id=resp.data["id"])
    assert list(expense.tags.values_list("id", flat=True)) == [tag.id]


def test_tag_cost_endpoint_sums_both_llm_calls_and_expenses_under_that_tag(client):
    tag = Tag.objects.create(key="project", value="reasoner")
    other_tag = Tag.objects.create(key="project", value="unrelated")

    run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    run.tags.add(tag)
    LLMCall.objects.create(run=run, provider="deepseek", model="m", cost_usd=Decimal("2.00"), billing="api")

    unrelated_run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    unrelated_run.tags.add(other_tag)
    LLMCall.objects.create(run=unrelated_run, provider="deepseek", model="m", cost_usd=Decimal("50.00"), billing="api")

    expense = Expense.objects.create(source="runpod", category="gpu-pod", amount_usd=Decimal("1.50"))
    expense.tags.add(tag)
    Expense.objects.create(source="runpod", category="gpu-pod", amount_usd=Decimal("77.00"))  # untagged

    resp = client.get(f"/api/tags/{tag.id}/cost/")
    assert resp.status_code == 200, resp.data
    assert resp.data["llm_cost_usd"] == "2.000000"
    assert resp.data["expense_cost_usd"] == "1.500000"
    assert resp.data["total_cost_usd"] == "3.500000"


def test_cost_summary_requires_at_least_one_tag_pair(client):
    resp = client.get("/api/cost-summary/")
    assert resp.status_code == 400


def test_cost_summary_ands_across_multiple_tag_pairs_not_ors(client):
    """A run tagged project=reasoner AND type=training must be counted; a run tagged ONLY
    project=reasoner (no type=training) must not, when both pairs are asked for -- the real bug this
    guards against is a naive single-filter() call that ORs instead of ANDs across an M2M."""
    project_tag = Tag.objects.create(key="project", value="reasoner")
    training_tag = Tag.objects.create(key="type", value="training")
    other_type_tag = Tag.objects.create(key="type", value="inference")

    matching_run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    matching_run.tags.add(project_tag, training_tag)
    LLMCall.objects.create(run=matching_run, provider="x", model="m", cost_usd=Decimal("10.00"), billing="api")

    partial_run = Run.objects.create(run_id=uuid.uuid4(), project="p")
    partial_run.tags.add(project_tag, other_type_tag)  # same project, different type -- must NOT match
    LLMCall.objects.create(run=partial_run, provider="x", model="m", cost_usd=Decimal("999.00"), billing="api")

    resp = client.get("/api/cost-summary/", {"key": ["project", "type"], "value": ["reasoner", "training"]})
    assert resp.status_code == 200, resp.data
    assert resp.data["run_count"] == 1
    assert resp.data["llm_cost_usd"] == "10.000000"


def test_a_comment_can_be_added_to_an_expense_and_listed_back(client):
    expense = Expense.objects.create(source="runpod", category="gpu-pod", amount_usd=Decimal("12.00"))

    resp = client.post(f"/api/expenses/{expense.id}/comments/", {"author": "elvis", "text": "this spike was the grokking repro run"}, format="json")
    assert resp.status_code == 201, resp.data

    listing = client.get(f"/api/expenses/{expense.id}/comments/")
    assert listing.status_code == 200
    assert len(listing.data) == 1
    assert listing.data[0]["text"] == "this spike was the grokking repro run"

    full_expense = client.get(f"/api/expenses/{expense.id}/")
    assert len(full_expense.data["comments"]) == 1
