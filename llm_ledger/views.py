"""Every viewset declares `filterset_fields` explicitly, AND `filter_backends` locally (not relying on a
project-wide DEFAULT_FILTER_BACKENDS setting), because a query param that isn't a declared filter is
silently ignored by DRF/django-filter and returns the WHOLE unfiltered queryset instead of erroring -- a
real, repeated incident in the project this app was generalized from. Self-contained here so embedding
this app in another project's settings can't accidentally lose the filter backend and reintroduce it."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import LLMCall, ModelPricing, Run
from .pricing import NoPricingError, compute_cost, find_pricing
from .serializers import LLMCallSerializer, ModelPricingSerializer, RunSerializer


class ModelPricingViewSet(viewsets.ModelViewSet):
    queryset = ModelPricing.objects.all()
    serializer_class = ModelPricingSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["model", "verification"]


class RunViewSet(viewsets.ModelViewSet):
    queryset = Run.objects.all()
    serializer_class = RunSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["project", "label", "purpose", "status"]

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
