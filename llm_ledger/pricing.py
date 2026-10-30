"""Cost computation from ModelPricing -- generalized from agentic_doc.telemetry's server-side cost
calculation (the same logic agentic-doc-treatement's Django server already runs; moved here so the
reusable app owns it, not a copy living in every project that embeds this app)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.db.models import Q

from .models import ModelPricing, Provider, ProviderPricingPolicy


class NoPricingError(ValueError):
    """No ModelPricing row covers this model at this date -- raised rather than silently costing $0,
    since a silent $0 looks identical to a real subscription call with no marginal cost."""


def find_provider_policy(provider: Provider, at: datetime | date) -> ProviderPricingPolicy | None:
    """The policy that was in effect for this provider AT `at` -- never "whatever the provider's
    policy is today". None if no policy row covers that date (the provider has never had a peak/
    off-peak schedule, or didn't yet at that point in time). Same time-boxed lookup shape as
    find_pricing(), deliberately: a policy that changes over time needs the exact same discipline as
    a price that changes over time, for the exact same reason."""
    d = at.date() if isinstance(at, datetime) else at
    return (
        ProviderPricingPolicy.objects.filter(provider=provider, effective_from__lte=d)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gte=d))
        .order_by("-effective_from")
        .first()
    )


def _effective_offpeak_multiplier(pricing: ModelPricing, at: datetime) -> Decimal | None:
    """A ModelPricing row's own value wins when set (a model-specific override); otherwise falls back
    to whatever policy its Provider had in effect AT `at` -- the peak/off-peak STRATEGY belongs to the
    provider and can change over time, same as a price can."""
    if pricing.offpeak_multiplier is not None:
        return pricing.offpeak_multiplier
    if not pricing.provider_id:
        return None
    policy = find_provider_policy(pricing.provider, at)
    return policy.offpeak_multiplier if policy else None


def _effective_peak_windows(pricing: ModelPricing, at: datetime) -> dict | None:
    if pricing.peak_windows_utc:
        return pricing.peak_windows_utc
    if not pricing.provider_id:
        return None
    policy = find_provider_policy(pricing.provider, at)
    return policy.peak_windows_utc if policy else None


def is_peak(peak_windows_utc: dict | None, at: datetime) -> bool | None:
    """True/False given a peak_windows_utc dict and a timestamp; None if no windows are defined at all
    (peak/off-peak doesn't apply -- flat pricing). Shared by provider_is_peak() and pricing's own
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
    True/False using whatever policy was in effect AT `at` (default: now), None if no policy covers
    that date at all (flat pricing, or no policy existed yet)."""
    at = at or datetime.now(dt_timezone.utc)
    policy = find_provider_policy(provider, at)
    return is_peak(policy.peak_windows_utc, at) if policy else None


def find_pricing(model: str, at: datetime | date | None = None) -> ModelPricing:
    at = at or datetime.now(dt_timezone.utc)
    d = at.date() if isinstance(at, datetime) else at
    row = (
        ModelPricing.objects.filter(model=model, effective_from__lte=d, voided=False)
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gte=d))
        .order_by("-effective_from")
        .first()
    )
    if row is None:
        raise NoPricingError(f"no ModelPricing row covers model={model!r} as of {d}")
    return row


# usage_extra keys that are informational only: providers bill reasoning as output tokens and image
# input as input tokens, so both are already inside the token columns. Pricing them again would count
# the same tokens twice.
INFORMATIONAL_USAGE = frozenset({"reasoning_tokens", "image_input_tokens", "images"})
CACHE_WRITE_1H = "cache_write_1h_tokens"
_RATE_KEYS = ("input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok",
              "cache_write_usd_per_mtok", "cache_write_1h_usd_per_mtok")
_MTOK = Decimal("1000000")


@dataclass
class PricedCall:
    total: Decimal
    breakdown: dict
    complete: bool  # False when some usage had no price; total is then a lower bound


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _rates_for(pricing: ModelPricing, prompt_length: int) -> tuple[dict, str]:
    rates = {key: getattr(pricing, key) for key in _RATE_KEYS}
    tier_used = ""
    tiers = sorted(pricing.context_tiers or [], key=lambda t: int(t.get("above_prompt_tokens", 0)))
    for tier in tiers:
        threshold = int(tier.get("above_prompt_tokens", 0))
        if prompt_length > threshold:
            tier_used = f"prompt over {threshold} tokens"
            for key in _RATE_KEYS:
                if key in tier:
                    rates[key] = _dec(tier[key])
    return rates, tier_used


def price_call(
    pricing: ModelPricing,
    *,
    at: datetime,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    usage_extra: dict | None = None,
    modifiers: list[str] | None = None,
) -> PricedCall:
    """Prices one call the way providers bill it: input, output, cache reads, cache writes (5-minute
    and 1-hour at their own rates), the long-prompt tier, request modifiers (batch, fast, regional...)
    and off-peak stacked on every token component, and per-unit fees (web searches...) at face value.

    Usage with no rate on the row is never silently $0: it is listed under "unpriced" and the result
    is marked incomplete, so the gap is visible (issue #1 -- 1.49 billion DeepSeek cache-read tokens
    were costed $0 for two weeks because the row lacked a cache-read rate and nothing said so)."""
    usage_extra = dict(usage_extra or {})
    modifiers = list(modifiers or [])
    unpriced: list[str] = []
    prompt_length = prompt_tokens + cache_read_tokens + cache_creation_tokens
    rates, tier_used = _rates_for(pricing, prompt_length)

    write_1h = min(int(usage_extra.get(CACHE_WRITE_1H, 0) or 0), cache_creation_tokens)
    write_5m = cache_creation_tokens - write_1h
    token_parts = {}
    for name, tokens, rate_key in (
        ("input", prompt_tokens, "input_usd_per_mtok"),
        ("output", completion_tokens, "output_usd_per_mtok"),
        ("cache_read", cache_read_tokens, "cache_read_usd_per_mtok"),
        ("cache_write", write_5m, "cache_write_usd_per_mtok"),
        ("cache_write_1h", write_1h, "cache_write_1h_usd_per_mtok"),
    ):
        if not tokens:
            continue
        rate = rates[rate_key]
        if rate is None:
            unpriced.append(f"{name}: {tokens} tokens, no {rate_key} on pricing row {pricing.pk}")
            continue
        token_parts[name] = (Decimal(tokens) / _MTOK) * rate

    # Off-peak (the row's own multiplier, else its provider's policy at `at`) and request modifiers
    # all scale token prices and stack -- Anthropic states caching, batch and data-residency multipliers
    # stack; DeepSeek's off-peak discount applies to every token category.
    multipliers = []
    effective_multiplier = _effective_offpeak_multiplier(pricing, at)
    if effective_multiplier is not None and is_peak(_effective_peak_windows(pricing, at), at) is False:
        multipliers.append(("off-peak", effective_multiplier))
    known = pricing.modifier_multipliers or {}
    for mod in modifiers:
        if mod in known:
            multipliers.append((mod, _dec(known[mod])))
        else:
            unpriced.append(f"modifier {mod!r}: no multiplier on pricing row {pricing.pk}")
    factor = Decimal("1")
    for _, m in multipliers:
        factor *= m

    unit_parts = {}
    unit_prices = pricing.unit_prices or {}
    for unit, count in usage_extra.items():
        if unit == CACHE_WRITE_1H or unit in INFORMATIONAL_USAGE or not count:
            continue
        price = unit_prices.get(unit)
        if price is None:
            unpriced.append(f"{unit}: {count}, no unit price on pricing row {pricing.pk}")
            continue
        unit_parts[unit] = Decimal(str(count)) / Decimal(str(price.get("per", 1))) * Decimal(str(price["usd"]))

    q = Decimal("0.000001")
    parts = {k: (v * factor).quantize(q) for k, v in token_parts.items()}
    parts.update({k: v.quantize(q) for k, v in unit_parts.items()})
    total = sum(parts.values(), Decimal("0")).quantize(q)
    breakdown = {
        "pricing_row": pricing.pk,
        "parts_usd": {k: str(v) for k, v in parts.items()},
        "multipliers": [[name, str(m)] for name, m in multipliers],
        "total_usd": str(total),
    }
    if tier_used:
        breakdown["tier"] = tier_used
    if unpriced:
        breakdown["unpriced"] = unpriced
    return PricedCall(total=total, breakdown=breakdown, complete=not unpriced)


def compute_cost(
    pricing: ModelPricing,
    *,
    at: datetime,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """USD cost for one call's four token columns -- price_call(...).total; kept for callers that only
    need the number."""
    return price_call(
        pricing, at=at, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens, cache_creation_tokens=cache_creation_tokens,
    ).total


def pricing_fields(
    *,
    model: str,
    at: datetime,
    billing: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    usage_extra: dict | None = None,
    modifiers: list[str] | None = None,
    claimed_api_equivalent: Decimal | None = None,
) -> dict:
    """The server-computed LLMCall fields for one call -- shared by call creation and reprice_calls so
    there is one pricing path. A subscription call keeps cost_usd at 0 (no marginal cash) and gets its
    list-price equivalent in api_equivalent_cost_usd (issue #3); a client's own claimed equivalent is
    only kept when the ledger has no price to compute one from."""
    try:
        pricing = find_pricing(model, at)
    except NoPricingError as exc:
        fields = {"pricing_used": None, "cost_breakdown": {"unpriced": [str(exc)]}, "cost_complete": False}
        if billing == "subscription":
            fields.update(cost_usd=Decimal("0"), api_equivalent_cost_usd=claimed_api_equivalent)
        else:
            fields["cost_usd"] = Decimal("0")
        return fields
    priced = price_call(
        pricing, at=at, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens, cache_creation_tokens=cache_creation_tokens,
        usage_extra=usage_extra, modifiers=modifiers,
    )
    fields = {"pricing_used": pricing, "cost_breakdown": priced.breakdown, "cost_complete": priced.complete}
    if billing == "subscription":
        fields.update(cost_usd=Decimal("0"), api_equivalent_cost_usd=priced.total)
    else:
        fields["cost_usd"] = priced.total
    return fields


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
