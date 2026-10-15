"""Mountable via include("llm_ledger.urls") from a host project's own urls.py, or used as-is in
standalone mode (see llm_ledger/standalone/). Never assumes it owns the URL root."""

from rest_framework.routers import DefaultRouter

from .views import CostSummaryView, ExpenseViewSet, LLMCallViewSet, ModelPricingViewSet, RunViewSet, TagViewSet

router = DefaultRouter()
router.register("model-pricing", ModelPricingViewSet, basename="model-pricing")
router.register("tags", TagViewSet, basename="tag")
router.register("runs", RunViewSet, basename="run")
router.register("calls", LLMCallViewSet, basename="llm-call")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("cost-summary", CostSummaryView, basename="cost-summary")

urlpatterns = router.urls
