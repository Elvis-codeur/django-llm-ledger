from django.urls import include, path

urlpatterns = [
    path("api/", include("llm_ledger.urls")),
]
