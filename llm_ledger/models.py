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

# Shared by ModelPricing and ProviderPricingPolicy -- a price/policy is only as good as where it came
# from, for either shape of historical row.
VERIFICATION_CHOICES = [
    ("primary", "Provider's own page or announcement"),
    ("secondary", "Third-party report, not confirmed on the provider's page"),
    ("unverified", "Recorded, never checked"),
]


class Provider(models.Model):
    """One API/compute provider (e.g. "deepseek", "anthropic", "runpod"), independent of any one
    model -- the same provider offers several models (DeepSeek has deepseek-flash AND deepseek-v4-pro;
    the same model id can even be hosted by more than one provider, e.g. a model available both direct
    from its maker and via a gateway). This row is PURE IDENTITY -- name/display/notes -- and never
    carries the peak/off-peak schedule itself: see `ProviderPricingPolicy` for why, and `ModelPricing`
    for why the same time-boxing matters for a single number, let alone a whole policy a provider can
    change."""

    name = models.CharField(max_length=64, unique=True, help_text='short id, e.g. "deepseek", "runpod"')
    display_name = models.CharField(max_length=128, blank=True, default="")
    website_url = models.URLField(blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.display_name or self.name


class ProviderPricingPolicy(models.Model):
    """One historical peak/off-peak POLICY for one Provider, valid over [effective_from,
    effective_until) -- the exact same time-boxing as ModelPricing, for the exact same reason, and
    this is not hypothetical: a provider changing its peak-hour policy is a real, expected event (a
    user caught this gap live, 2026-10-15, before any real drift happened -- editing the policy in
    place would have silently recomputed the peak/off-peak status of every PAST call under whatever
    policy is current today, which is wrong the moment a provider ever changes its schedule). A policy
    CHANGE is always a new row with a new effective_from; an old row is never edited, same discipline
    as ModelPricing's own docstring. `ModelPricing.offpeak_multiplier`/`peak_windows_utc` still exist
    for a model that genuinely needs its own override of its provider's policy; absent that, cost
    computation looks up the Provider's policy that was in effect AT THE CALL'S OWN TIMESTAMP, never
    "whatever the provider's policy is today" (see pricing.find_provider_policy)."""

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="pricing_policies")
    offpeak_multiplier = models.DecimalField(max_digits=5, decimal_places=4)
    # Same format as ModelPricing.peak_windows_utc: {"weekdays": [0..6], "hours": [[start, end), ...]}.
    peak_windows_utc = models.JSONField()
    effective_from = models.DateField()
    effective_until = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=256, blank=True, default="")
    source_url = models.URLField(max_length=500, blank=True, default="")
    verification = models.CharField(max_length=16, choices=VERIFICATION_CHOICES, blank=True, default="")
    verified_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["provider__name", "effective_from"]

    def __str__(self) -> str:
        return f"{self.provider.name} policy ({self.effective_from}..{self.effective_until or 'now'})"


class ModelPricing(models.Model):
    """One historical price row for one model string, valid over [effective_from, effective_until).
    Multiple rows for the same model with non-overlapping date ranges are how a price CHANGE over time is
    represented -- a cost computed for a call made last month must use the price that was actually in
    effect then, not today's price. See `compute_cost()` below."""

    model = models.CharField(max_length=128)
    # Nullable, SET_NULL: existing rows predate this field and `model` (often already "provider/id")
    # remains the source of truth for cost computation either way -- this FK is for the peak/off-peak
    # fallback and for querying "every pricing row for provider X" without string-matching `model`.
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="pricing_rows")
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
    # Append-only, same discipline as Expense: a row recorded with a wrong date or price is voided
    # (kept, with why), never edited or deleted -- even before any real call has used it, so the habit
    # never erodes into "but nothing depends on this one yet." The correction is always a NEW row.
    voided = models.BooleanField(default=False)
    void_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["model", "effective_from"]

    def __str__(self) -> str:
        return f"{self.model} ({self.effective_from}..{self.effective_until or 'now'})"


class Tag(models.Model):
    """A key/value label for cost attribution (e.g. key="project", value="agentic-reasoning-translator",
    or key="type", value="training") -- AWS-style cost-allocation tags, not a single flat string, so a
    Run or Expense can carry several independent dimensions (which project, what kind of work, which
    experiment) and be queried by any one of them without the others colliding."""

    key = models.CharField(max_length=64, db_index=True)
    value = models.CharField(max_length=128, db_index=True)

    class Meta:
        ordering = ["key", "value"]
        constraints = [
            models.UniqueConstraint(fields=["key", "value"], name="llm_ledger_unique_tag_kv"),
        ]

    def __str__(self) -> str:
        return f"{self.key}:{self.value}"


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
    tags = models.ManyToManyField(Tag, blank=True, related_name="runs")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.project}/{self.label or 'run'} ({self.run_id})"


class RunStep(models.Model):
    """One named phase within a Run's wall-clock time (e.g. "data_prep", "epoch_1_train",
    "epoch_1_eval", "checkpoint_save") -- the GPU-time equivalent of LLMCall.call_purpose's per-step
    cost attribution, for a job whose cost is GPU-rental time, not per-token API calls (an SFT
    training run has no LLMCall rows at all; its real cost is an Expense keyed to the Run). A step has
    no cost of its own -- cost is APPORTIONED after the fact, by `RunViewSet.step_costs`, as
    (this step's duration / the run's total step duration) * the run's total real Expense cost. This
    generalizes the pattern already proven in agentic-doc-treatement (LLMCall.call_purpose, used to
    find where an STT/translation job's cost concentrated) to the GPU-time case, where there's no
    per-call row to group by -- only wall-clock phase boundaries the training script itself reports."""

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="steps")
    label = models.CharField(max_length=128)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=256, blank=True, default="")

    class Meta:
        ordering = ["run", "started_at"]

    def __str__(self) -> str:
        return f"{self.run_id}/{self.label}"


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


class Expense(models.Model):
    """Any real cost that is NOT a per-token LLM API call -- GPU rental (RunPod pods/endpoints),
    storage, network volumes, a flat subscription fee, anything billed in dollars that this ledger
    should still account for. Deliberately NOT modelled as an LLMCall: an LLMCall has tokens, a model,
    pricing provenance; a GPU-hour or a storage-GB-month has none of that and forcing it through the
    same shape would mean a pile of meaningless null token columns. `source` names the billing system
    (e.g. "runpod"), `category` names the kind of resource within it (e.g. "gpu-pod",
    "network-volume", "serverless-endpoint") -- both free text, since this app has no fixed opinion on
    every provider's resource taxonomy, same reasoning as Run.project/label."""

    source = models.CharField(max_length=64, db_index=True)
    category = models.CharField(max_length=64, blank=True, default="")
    amount_usd = models.DecimalField(max_digits=12, decimal_places=6)
    occurred_at = models.DateTimeField(default=timezone.now)
    description = models.CharField(max_length=256, blank=True, default="")
    # Caller-supplied idempotency key (e.g. a RunPod billing record id) -- unique per source when
    # non-empty, so re-running an importer against the same billing window cannot double-insert. Same
    # pattern as LLMCall.external_id.
    external_id = models.CharField(max_length=160, blank=True, default="")
    # The raw billing record as returned by the source system, kept for provenance/debugging -- never
    # trusted over amount_usd, which is the one field actually summed for reporting.
    raw = models.JSONField(default=dict, blank=True)
    run = models.ForeignKey(Run, on_delete=models.SET_NULL, null=True, blank=True, related_name="expenses")
    tags = models.ManyToManyField(Tag, blank=True, related_name="expenses")
    # A ledger should be append-only, like real bookkeeping: a wrong entry gets VOIDED and a corrected
    # one posted alongside it, never hard-deleted -- deleting a transaction erases the audit trail of
    # what was believed true and when. `voided` rows are excluded from cost-summary/timeseries/
    # distribution by default (pass include_voided=1 to see them), but stay in the table and keep their
    # comments. `superseded_by` links a voided row to whatever replaced it, so the correction is
    # traceable instead of just "this number disappeared."
    voided = models.BooleanField(default=False)
    void_reason = models.TextField(blank=True, default="")
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="supersedes")

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~models.Q(external_id=""),
                name="llm_ledger_unique_external_id_per_source",
            )
        ]

    def __str__(self) -> str:
        return f"{self.source}/{self.category or 'expense'} @ {self.occurred_at} (${self.amount_usd})"


class ExpenseComment(models.Model):
    """A free-text note attached to one Expense, for exactly the case a flat billing import can't
    capture on its own: a weird or unexplained cost that a human or an LLM looked into later and wants
    to leave a record of -- what it actually was, which project it really belongs to, whether it was a
    mistake -- so the NEXT person or model to look at this ledger doesn't have to re-investigate from
    scratch. A separate model (not a single growing text field on Expense) because more than one note
    can accumulate over time, each with its own author and timestamp, and a comment should be addable
    without re-PUTting the whole Expense row."""

    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="comments")
    author = models.CharField(max_length=64, blank=True, default="", help_text="free text, e.g. a human's name or an agent/model id")
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"comment on expense {self.expense_id} by {self.author or 'unknown'}"
