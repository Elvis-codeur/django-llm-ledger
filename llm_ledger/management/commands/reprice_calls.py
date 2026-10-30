"""Re-price recorded LLM calls with the pricing rows in effect now (issue #4): after a pricing
correction (a row voided and replaced), calls already recorded still carry the old cost. A call's
cost is derived from its usage and the price table, so recomputing it is a correction of a derived
figure, not an edit of a record; the previous figure is kept in the call's cost_breakdown."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from llm_ledger.models import LLMCall
from llm_ledger.pricing import pricing_fields


class Command(BaseCommand):
    help = "Recompute cost_usd / api_equivalent_cost_usd / cost_breakdown for recorded calls."

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", default=[], help="only this model (repeatable)")
        parser.add_argument("--since", help="only calls at or after this ISO date/time")
        parser.add_argument("--until", help="only calls before this ISO date/time")
        parser.add_argument("--incomplete-only", action="store_true", help="only calls with cost_complete=False")
        parser.add_argument("--dry-run", action="store_true", help="report the change, write nothing")

    def handle(self, *args, **opts):
        calls = LLMCall.objects.all().order_by("pk")
        if opts["model"]:
            calls = calls.filter(model__in=opts["model"])
        if opts["since"]:
            calls = calls.filter(timestamp__gte=datetime.fromisoformat(opts["since"]))
        if opts["until"]:
            calls = calls.filter(timestamp__lt=datetime.fromisoformat(opts["until"]))
        if opts["incomplete_only"]:
            calls = calls.filter(cost_complete=False)

        per_model = defaultdict(lambda: {"calls": 0, "changed": 0, "before": Decimal("0"), "after": Decimal("0"),
                                         "incomplete_after": 0})
        repriced_at = datetime.now(dt_timezone.utc).isoformat()
        with transaction.atomic():
            for call in calls.iterator(chunk_size=500):
                is_sub = call.billing == "subscription"
                before = (call.api_equivalent_cost_usd or Decimal("0")) if is_sub else call.cost_usd
                fields = pricing_fields(
                    model=call.model, at=call.timestamp, billing=call.billing,
                    prompt_tokens=call.prompt_tokens, completion_tokens=call.completion_tokens,
                    cache_read_tokens=call.cache_read_tokens, cache_creation_tokens=call.cache_creation_tokens,
                    usage_extra=call.usage_extra, modifiers=call.pricing_modifiers,
                    claimed_api_equivalent=call.api_equivalent_cost_usd,
                )
                after = (fields.get("api_equivalent_cost_usd") or Decimal("0")) if is_sub else fields["cost_usd"]
                stats = per_model[call.model]
                stats["calls"] += 1
                stats["before"] += before
                stats["after"] += after
                stats["incomplete_after"] += 0 if fields["cost_complete"] else 1
                if after == before and fields["cost_complete"] == call.cost_complete and call.cost_breakdown:
                    continue
                stats["changed"] += 1
                if opts["dry_run"]:
                    continue
                breakdown = dict(fields["cost_breakdown"])
                breakdown["previous"] = {
                    "cost_usd": str(call.cost_usd),
                    "api_equivalent_cost_usd": str(call.api_equivalent_cost_usd)
                    if call.api_equivalent_cost_usd is not None else None,
                    "pricing_row": call.pricing_used_id,
                    "repriced_at": repriced_at,
                }
                fields["cost_breakdown"] = breakdown
                for name, value in fields.items():
                    setattr(call, name, value)
                call.save(update_fields=list(fields))
            if opts["dry_run"]:
                transaction.set_rollback(True)

        verb = "would change" if opts["dry_run"] else "changed"
        total_before = total_after = Decimal("0")
        for model, s in sorted(per_model.items()):
            total_before += s["before"]
            total_after += s["after"]
            still = f", {s['incomplete_after']} still incomplete" if s["incomplete_after"] else ""
            self.stdout.write(
                f"{model}: {s['calls']} calls, {verb} {s['changed']}, ${s['before']:.4f} -> ${s['after']:.4f}{still}"
            )
        self.stdout.write(f"TOTAL: ${total_before:.4f} -> ${total_after:.4f}" + (" (dry run, nothing written)"
                                                                               if opts["dry_run"] else ""))
