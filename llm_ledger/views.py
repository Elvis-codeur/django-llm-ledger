"""Every viewset declares `filterset_fields` explicitly, AND `filter_backends` locally (not relying on a
project-wide DEFAULT_FILTER_BACKENDS setting), because a query param that isn't a declared filter is
silently ignored by DRF/django-filter and returns the WHOLE unfiltered queryset instead of erroring -- a
real, repeated incident in the project this app was generalized from. Self-contained here so embedding
this app in another project's settings can't accidentally lose the filter backend and reintroduce it."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Expense, ExpenseComment, LLMCall, ModelPricing, Run, Tag
from .pricing import NoPricingError, compute_cost, find_pricing
from .serializers import (
    ExpenseCommentSerializer,
    ExpenseSerializer,
    LLMCallSerializer,
    ModelPricingSerializer,
    RunSerializer,
    TagSerializer,
)


class ModelPricingViewSet(viewsets.ModelViewSet):
    queryset = ModelPricing.objects.all()
    serializer_class = ModelPricingSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["model", "verification"]


class TagViewSet(viewsets.ModelViewSet):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["key", "value"]

    def create(self, request, *args, **kwargs):
        """Idempotent by (key, value): the same tag named twice (e.g. two different importer runs both
        wanting "project:agentic-reasoning-translator") returns the existing row (200), same pattern as
        Run's run_id idempotency below -- a tag is an identity, not an event, so a duplicate POST is a
        lookup, not an error."""
        key, value = request.data.get("key"), request.data.get("value")
        if key and value:
            existing = Tag.objects.filter(key=key, value=value).first()
            if existing is not None:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=["get"])
    def cost(self, request, pk=None):
        """Total real spend under this one tag, across BOTH per-token LLM calls (via any Run tagged
        with it) and flat Expense rows (GPU rental, storage, etc.) tagged with it directly -- the whole
        point of tagging: one number for "everything billed under this project/experiment/whatever",
        regardless of which of the two cost shapes it came from."""
        tag = self.get_object()
        api_calls = LLMCall.objects.filter(run__tags=tag, billing="api")
        llm_cost = sum((c.cost_usd for c in api_calls), start=0)
        expenses = Expense.objects.filter(tags=tag)
        expense_cost = sum((e.amount_usd for e in expenses), start=0)
        return Response(
            {
                "tag": str(tag),
                "llm_cost_usd": str(llm_cost),
                "llm_call_count": api_calls.count(),
                "expense_cost_usd": str(expense_cost),
                "expense_count": expenses.count(),
                "total_cost_usd": str(llm_cost + expense_cost),
            }
        )


class RunViewSet(viewsets.ModelViewSet):
    queryset = Run.objects.all()
    serializer_class = RunSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "label", "purpose", "status", "run_id", "tags"]

    def create(self, request, *args, **kwargs):
        """Idempotent by run_id: a client that queued a run locally (see llm-ledger-client's
        fallback-then-flush design) may legitimately POST the same run_id twice -- once live, once on a
        later flush that doesn't know the first attempt already landed. A second POST for a run_id that
        already exists returns the EXISTING row (200), not a 400 -- the same real gap LLMCall already
        closes with its own external_id uniqueness, found by an actual cross-repo integration test
        (llm-ledger-client + this app) rather than assumed up front."""
        run_id = request.data.get("run_id")
        if run_id:
            existing = Run.objects.filter(run_id=run_id).first()
            if existing is not None:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=["get"])
    def cost(self, request, pk=None):
        """Real-money total for this run: sum of billing="api" calls' cost_usd, plus the
        subscription-covered calls' token counts and api_equivalent_cost_usd kept SEPARATE (never
        folded into the real total -- see LLMCall.billing's docstring)."""
        run = self.get_object()
        api_calls = run.calls.filter(billing="api")
        sub_calls = run.calls.filter(billing="subscription")
        return Response(
            {
                "run_id": str(run.run_id),
                "real_cost_usd": str(sum((c.cost_usd for c in api_calls), start=0)),
                "api_call_count": api_calls.count(),
                "subscription_call_count": sub_calls.count(),
                "subscription_api_equivalent_cost_usd": str(
                    sum((c.api_equivalent_cost_usd or 0 for c in sub_calls), start=0)
                ),
            }
        )


class LLMCallViewSet(viewsets.ModelViewSet):
    queryset = LLMCall.objects.all()
    serializer_class = LLMCallSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["run", "provider", "model", "billing", "status", "external_id"]

    def perform_create(self, serializer):
        """Computes cost_usd/pricing_used from ModelPricing server-side -- a client posts raw token
        counts, never a claimed cost, so pricing drift or a client bug can't silently misreport spend.
        billing="subscription" calls are never priced (cost_usd stays 0; that is correct, not a gap --
        see LLMCall.billing)."""
        data = serializer.validated_data
        if data.get("billing") == "subscription":
            serializer.save()
            return
        at = data.get("timestamp") or datetime.now(dt_timezone.utc)
        try:
            pricing = find_pricing(data["model"], at)
        except NoPricingError:
            serializer.save()  # cost_usd stays 0 -- an absent price is a data gap to fill, not a reason to refuse the write
            return
        cost = compute_cost(
            pricing,
            at=at,
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_creation_tokens=data.get("cache_creation_tokens", 0),
        )
        serializer.save(cost_usd=cost, pricing_used=pricing)


class ExpenseViewSet(viewsets.ModelViewSet):
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["source", "category", "external_id", "run", "tags"]

    def create(self, request, *args, **kwargs):
        """Idempotent by (source, external_id) when external_id is given: a RunPod billing importer run
        twice over the same window must not double-count real GPU spend -- same shape as LLMCall's own
        external_id uniqueness, enforced here at the API layer too (not just the DB constraint) so a
        re-import gets back the existing row (200) instead of a 400."""
        source, external_id = request.data.get("source"), request.data.get("external_id")
        if source and external_id:
            existing = Expense.objects.filter(source=source, external_id=external_id).first()
            if existing is not None:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        """GET lists this expense's comments; POST adds one ({"author": "...", "text": "..."}) --
        exactly the "leave a note on a weird cost for later" path, addable without re-PUTting the whole
        Expense row."""
        expense = self.get_object()
        if request.method == "GET":
            return Response(ExpenseCommentSerializer(expense.comments.all(), many=True).data)
        serializer = ExpenseCommentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(expense=expense)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class CostSummaryView(viewsets.ViewSet):
    """One place to answer "how much have I spent on X", where X is any set of tags -- not tied to one
    Run or one Expense, across the whole ledger. `?key=project&value=agentic-reasoning-translator`
    (repeatable) ANDs across distinct keys (every matching tag's key/value pair must be present on the
    same Run or Expense) so "project=X AND type=training" narrows correctly instead of OR-ing."""

    def list(self, request):
        pairs = list(zip(request.query_params.getlist("key"), request.query_params.getlist("value")))
        if not pairs:
            return Response({"error": "pass at least one ?key=...&value=... pair"}, status=status.HTTP_400_BAD_REQUEST)

        # Each pair needs its own `.filter()` call (a separate join to the M2M table) rather than one
        # combined Q object -- a single joined row can't simultaneously be tag A and tag B, so ANDing
        # within one filter() would always return nothing for more than one pair.
        runs = Run.objects.all()
        expenses = Expense.objects.all()
        for key, value in pairs:
            runs = runs.filter(tags__key=key, tags__value=value)
            expenses = expenses.filter(tags__key=key, tags__value=value)
        runs = runs.distinct()
        expenses = expenses.distinct()

        api_calls = LLMCall.objects.filter(run__in=runs, billing="api")
        llm_cost = sum((c.cost_usd for c in api_calls), start=0)
        expense_cost = sum((e.amount_usd for e in expenses), start=0)
        return Response(
            {
                "tags": [{"key": k, "value": v} for k, v in pairs],
                "run_count": runs.count(),
                "llm_cost_usd": str(llm_cost),
                "llm_call_count": api_calls.count(),
                "expense_cost_usd": str(expense_cost),
                "expense_count": expenses.count(),
                "total_cost_usd": str(llm_cost + expense_cost),
            }
        )
