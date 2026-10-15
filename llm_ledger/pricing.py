"""Cost computation from ModelPricing -- generalized from agentic_doc.telemetry's server-side cost
calculation (the same logic agentic-doc-treatement's Django server already runs; moved here so the
reusable app owns it, not a copy living in every project that embeds this app)."""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.db.models import Q

from .models import ModelPricing, Provider


class NoPricingError(ValueError):
    """No ModelPricing row covers this model at this date -- raised rather than silently costing $0,
    since a silent $0 looks identical to a real subscription call with no marginal cost."""


def _effective_offpeak_multiplier(pricing: ModelPricing) -> Decimal | None:
    """A ModelPricing row's own value wins when set (a model-specific override); otherwise falls back
    to its Provider's value -- the peak/off-peak STRATEGY belongs to the provider, not each model."""
    if pricing.offpeak_multiplier is not None:
        return pricing.offpeak_multiplier
    return pricing.provider.offpeak_multiplier if pricing.provider_id else None


def _effective_peak_windows(pricing: ModelPricing) -> dict | None:
    if pricing.peak_windows_utc:
        return pricing.peak_windows_utc
    return pricing.provider.peak_windows_utc if pricing.provider_id else None


def is_peak(peak_windows_utc: dict | None, at: datetime) -> bool | None:
    """True/False given a peak_windows_utc dict and a timestamp; None if no windows are defined at all
    (peak/off-peak doesn't apply -- flat pricing). Shared by Provider.is_peak_at() and pricing's own
    off-peak check so there is exactly one place that reads this format."""
    if not peak_windows_utc:
        return None
    at_utc = at.astimezone(dt_timezone.utc)
    weekdays = peak_windows_utc.get("weekdays", [])
    if at_utc.weekday() not in weekdays:
        return False
    hour = at_utc.hour
    for start, end in peak_windows_utc.get("hours", []):
        if start <= hour < end:
            return True
    return False


def provider_is_peak(provider: Provider, at: datetime | None = None) -> bool | None:
    """The "accountancy map -- at which hour are we" check for one provider, independent of any model:
    True/False if the provider has a defined peak/off-peak schedule, None if it has flat pricing (the
    question doesn't apply). `at` defaults to now."""
    return is_peak(provider.peak_windows_utc, at or datetime.now(dt_timezone.utc))


def find_pricing(model: str, at: datetime | date | None = None) -> ModelPricing:
    at = at or datetime.now(dt_timezone.utc)
    d = at.date() if isinstance(at, datetime) else at
    row = (
        ModelPricing.objects.filter(model=model, effective_from__lte=d)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gte=d))
        .order_by("-effective_from")
        .first()
    )
    if row is None:
        raise NoPricingError(f"no ModelPricing row covers model={model!r} as of {d}")
    return row


def compute_cost(
    pricing: ModelPricing,
    *,
    at: datetime,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """USD cost for one call, applying the off-peak multiplier (the pricing row's own, falling back to
    its Provider's -- see `_effective_offpeak_multiplier`) based on `at`'s UTC weekday/hour against
    `peak_windows_utc`."""
    effective_multiplier = _effective_offpeak_multiplier(pricing)
    effective_windows = _effective_peak_windows(pricing)
    peak = is_peak(effective_windows, at) if effective_multiplier is not None else None
    # peak is None (no windows defined) or True (currently peak) -> full price. Only a confirmed
    # False (currently off-peak) applies the multiplier -- identical behavior to the old _is_offpeak,
    # just reusing the shared is_peak() so there is one definition of the time-window format.
    multiplier = effective_multiplier if (effective_multiplier is not None and peak is False) else Decimal("1")
    mtok = Decimal("1000000")
    total = (Decimal(prompt_tokens) / mtok) * pricing.input_usd_per_mtok
    total += (Decimal(completion_tokens) / mtok) * pricing.output_usd_per_mtok
    if cache_read_tokens and pricing.cache_read_usd_per_mtok is not None:
        total += (Decimal(cache_read_tokens) / mtok) * pricing.cache_read_usd_per_mtok
    if cache_creation_tokens and pricing.cache_write_usd_per_mtok is not None:
        total += (Decimal(cache_creation_tokens) / mtok) * pricing.cache_write_usd_per_mtok
    return (total * multiplier).quantize(Decimal("0.000001"))


def pricing_covers_date(model: str, as_of: date | None = None) -> bool:
    """True if a ModelPricing row for `model` covers `as_of` (default: today) -- call this before a real
    paid run to check pricing is current, same discipline as the owning project's own
    translate-document/verify-stt-transcript skills already require."""
    try:
        find_pricing(model, as_of)
        return True
    except NoPricingError:
        return False


def add_model_pricing(
    model: str,
    *,
    input_usd_per_mtok: Decimal | float,
    output_usd_per_mtok: Decimal | float,
    effective_from: date | None = None,
    note: str = "",
    source_url: str = "",
    verification: str = "unverified",
    **extra,
) -> ModelPricing:
    """Appends a new ModelPricing row -- never edits an existing one (a price change over time is a new
    row with a new effective_from, not a mutation of history)."""
    return ModelPricing.objects.create(
        model=model,
        input_usd_per_mtok=input_usd_per_mtok,
        output_usd_per_mtok=output_usd_per_mtok,
        effective_from=effective_from or date.today(),
        note=note,
        source_url=source_url,
        verification=verification,
        verified_at=date.today() if source_url else None,
        **extra,
    )
