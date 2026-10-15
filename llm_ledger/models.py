"""Generalized from agentic-doc-treatement/server/runs/models.py's ModelPricing/PipelineRun/LLMCall
(2026-10-15) -- the same design, with this project's own domain-specific concepts removed so it's reusable
from any project: Document (a translated source document) is replaced by a plain `project` string on Run,
and Run's fixed STAGE_CHOICES (extraction/correction/translation/...) becomes a free-form `label`, since a
generic cost ledger shouldn't hardcode one project's pipeline stages. Everything else -- the historical,
time-boxed pricing table with peak/off-peak support and provenance, the run/call split, the
api-vs-subscription billing distinction -- is the same shape, proven across months of real production use
in that project."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class ModelPricing(models.Model):
    """One historical price row for one model string, valid over [effective_from, effective_until).
    Multiple rows for the same model with non-overlapping date ranges are how a price CHANGE over time is
    represented -- a cost computed for a call made last month must use the price that was actually in
    effect then, not today's price. See `compute_cost()` below."""

    model = models.CharField(max_length=128)
    input_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4)
    output_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4)
    cache_read_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    cache_write_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    # A 1-hour cache write may be priced differently from a shorter-lived one (seen on Anthropic: 2x input
    # vs 1.25x for the default). Null = same as cache_write_usd_per_mtok / not applicable.
    cache_write_1h_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    effective_from = models.DateField()
    effective_until = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=256, blank=True)
    # Providers with time-of-day pricing (e.g. DeepSeek: off-peak is half of peak). The four rates above
    # are the PEAK (list) rates; when offpeak_multiplier is set, a call whose timestamp falls outside
    # peak_windows_utc is priced at rate * multiplier. Both null = flat pricing.
    # peak_windows_utc format: {"weekdays": [0, 1, 2, 3, 4], "hours": [[1, 4], [6, 10]]} -- Monday=0, hour
    # ranges are [start, end) in UTC. Not modelled: provider-specific holiday exceptions.
    offpeak_multiplier = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    peak_windows_utc = models.JSONField(null=True, blank=True)

    # Provenance: a price is only as good as where it came from.
    VERIFICATION_CHOICES = [
        ("primary", "Provider's own page or announcement"),
        ("secondary", "Third-party report, not confirmed on the provider's page"),
        ("unverified", "Recorded, never checked"),
    ]
    source_url = models.URLField(max_length=500, blank=True)
    verified_at = models.DateField(null=True, blank=True)
    verification = models.CharField(max_length=16, choices=VERIFICATION_CHOICES, blank=True, default="")
    # Exact UTC instant for a price that changes mid-day. When null, the date columns above mean whole UTC
    # days (start of effective_from through end of effective_until, both inclusive).
    starts_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["model", "effective_from"]

    def __str__(self) -> str:
        return f"{self.model} ({self.effective_from}..{self.effective_until or 'now'})"


class Run(models.Model):
    """One unit of work worth tracking cost for -- a translation pass, a training job, a backfill script,
    whatever the calling project calls a 'run'. Generalized from agentic-doc-treatement's PipelineRun: no
    `document` FK (that was specific to a document-translation pipeline) -- instead, `project` is a plain
    string naming which calling project/app this run belongs to, since this app is meant to be shared
    across several unrelated codebases. `label` is likewise free text instead of a fixed choices list --
    this app has no opinion on what stages a project's own pipeline has."""

    PURPOSE_CHOICES = [
        ("test", "Test"),
        ("development", "Development"),
        ("calibration", "Calibration"),
        ("production", "Production"),
    ]
    STATUS_CHOICES = [
        ("running", "Running"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("partial", "Partial"),
    ]

    run_id = models.UUIDField(unique=True)
    project = models.CharField(max_length=128, db_index=True)
    label = models.CharField(max_length=128, blank=True, default="")
    purpose = models.CharField(max_length=32, choices=PURPOSE_CHOICES, default="development")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="running")
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.CharField(max_length=64, default="human")
    git_commit = models.CharField(max_length=40, blank=True)
    config = models.JSONField(default=dict, blank=True)
    result_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.project}/{self.label or 'run'} ({self.run_id})"


class LLMCall(models.Model):
    """One real (or subscription-covered) LLM API call. Essentially unchanged from
    agentic-doc-treatement's LLMCall -- this model was already fully generic, no project-specific concept
    in it."""

    STATUS_CHOICES = [("success", "Success"), ("error", "Error")]
    BILLING_CHOICES = [
        ("api", "API (metered)"),
        # Work done inside a flat-rate plan (e.g. a Claude Code / ChatGPT Plus session): tokens are
        # recorded, cost_usd stays 0 (no marginal cash spent), and api_equivalent_cost_usd optionally
        # holds what the same tokens would cost at list price -- kept in its own column so it can never
        # leak into a real-cost total.
        ("subscription", "Subscription (no marginal cost)"),
    ]

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="calls")
    timestamp = models.DateTimeField(default=timezone.now)
    provider = models.CharField(max_length=64)
    model = models.CharField(max_length=128)
    resolved_model = models.CharField(max_length=128, blank=True)
    call_purpose = models.CharField(max_length=64, blank=True, default="")
    params = models.JSONField(default=dict, blank=True)
    input_text = models.TextField(blank=True)
    output_text = models.TextField(blank=True)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    cache_creation_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    pricing_used = models.ForeignKey(ModelPricing, on_delete=models.SET_NULL, null=True, blank=True)
    billing = models.CharField(max_length=16, choices=BILLING_CHOICES, default="api")
    api_equivalent_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    # Caller-supplied idempotency key for a call imported from an external transcript after the fact
    # (e.g. a Pi/DeepSeek session file, a Claude Code transcript) -- unique per run when non-empty, so
    # re-importing the same transcript cannot double-insert.
    external_id = models.CharField(max_length=160, blank=True, default="")
    latency_ms = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="success")
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "external_id"],
                condition=~models.Q(external_id=""),
                name="llm_ledger_unique_external_id_per_run",
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider}/{self.model} @ {self.timestamp} (${self.cost_usd})"
