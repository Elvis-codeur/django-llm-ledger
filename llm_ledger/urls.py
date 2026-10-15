"""Mountable via include("llm_ledger.urls") from a host project's own urls.py, or used as-is in
standalone mode (see llm_ledger/standalone/). Never assumes it owns the URL root."""

from rest_framework.routers import DefaultRouter

from .views import LLMCallViewSet, ModelPricingViewSet, RunViewSet

router = DefaultRouter()
router.register("model-pricing", ModelPricingViewSet, basename="model-pricing")
router.register("runs", RunViewSet, basename="run")
router.register("calls", LLMCallViewSet, basename="llm-call")

urlpatterns = router.urls
