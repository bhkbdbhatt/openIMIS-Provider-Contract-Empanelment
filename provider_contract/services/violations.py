"""The empanelment / pricing gate and its violation queue.

The gate is **advisory, never punitive** (ADR-006). Nothing in this module
rejects, cancels, amends or deletes a claim. A negative verdict produces exactly
one side effect: a ``ClaimScopeViolation`` row. The claim stays exactly where it
was.

The gate itself is a read path. :func:`check_provider_empanelment` and
:func:`applicable_fee` never mutate and never raise on a negative result -- they
return a verdict object carrying a machine-readable ``reason_code``, so an
adjudicator sees *why* a claim fell outside the contract instead of just "no".

Routing a BLOCKING verdict into the claim module's Review stage is deliberately
*not* implemented here. That would mean writing to ``claim`` tables, and this
module's only sanctioned integration point for pricing is the deferred
``calcrule_provider_contract_scope`` calculation rule. The deferred module
decides whether to route; this module only records.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.signals import register_service_signal

from provider_contract.models import (
    ClaimScopeViolation,
    ContractFeeSchedule,
)

from .common import (
    active_contract_for,
    as_datetime,
    config_value,
    contracts_covering,
    resolve_fee,
)


def gate_enabled():
    """``pce_gate_enabled = False`` must make this module fully inert."""
    return bool(config_value("pce_gate_enabled", True))


def check_provider_empanelment(provider, service=None, on_date=None):
    """Verdict object describing whether ``provider`` was empanelled on a date.

    Returns a dict, never an exception: this is called while a claim is being
    priced, and a raised error here would abort claim processing for reasons
    that belong in a review queue, not in a traceback.

    Shape::

        {
            "empanelled": bool,
            "reason_code": str | None,
            "reason": human readable,
            "severity": "INFO" | "WARNING" | "BLOCKING",
            "contract": contract_number | None,
            "contract_uuid": str | None,
            "date_of_service": ISO date | None,
        }
    """
    on_date = as_datetime(on_date) or timezone.now()
    verdict = {
        "empanelled": False,
        "reason_code": None,
        "reason": "",
        "severity": ClaimScopeViolation.Severity.INFO,
        "contract": None,
        "contract_uuid": None,
        "date_of_service": on_date.date().isoformat(),
    }

    if provider is None:
        verdict.update(
            reason_code=ClaimScopeViolation.ReasonCode.NOT_EMPANELLED,
            reason="No provider supplied.",
            severity=ClaimScopeViolation.Severity.BLOCKING,
        )
        return verdict

    covering = list(contracts_covering(provider, on_date))
    if not covering:
        verdict.update(
            reason_code=ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT,
            reason=f"No contract of {provider.code} covers {verdict['date_of_service']}.",
            severity=ClaimScopeViolation.Severity.BLOCKING,
        )
        return verdict

    contract = active_contract_for(provider, on_date)
    if contract is None:
        statuses = ", ".join(sorted({c.get_status_display() for c in covering}))
        verdict.update(
            reason_code=ClaimScopeViolation.ReasonCode.CONTRACT_EXPIRED,
            reason=(
                f"{provider.code} has no ACTIVE contract on "
                f"{verdict['date_of_service']} (found: {statuses})."
            ),
            severity=ClaimScopeViolation.Severity.BLOCKING,
        )
        return verdict

    verdict["contract"] = contract.contract_number
    verdict["contract_uuid"] = str(contract.uuid)

    if service is not None and not _service_in_scope(contract, service, on_date):
        verdict.update(
            reason_code=ClaimScopeViolation.ReasonCode.OUTSIDE_SCOPE,
            reason=(
                f"Service {service.code} is outside the contracted scope of "
                f"{contract.contract_number}."
            ),
            severity=ClaimScopeViolation.Severity.WARNING,
        )
        return verdict

    verdict.update(empanelled=True, reason="Provider is empanelled and in scope.")
    return verdict


def _service_in_scope(contract, service, on_date):
    """True when the contract either says nothing about scope, or includes it.

    A contract with no ``ContractServiceCategory`` rows is treated as covering
    everything, because "no scope recorded" is the legacy default in openIMIS
    and silently treating it as "out of scope" would flag every claim from every
    migrated contract.
    """
    from provider_contract.models import ContractServiceCategory

    categories = ContractServiceCategory.objects.filter(contract=contract)
    if not categories.exists():
        return True
    category = getattr(service, "category", None)
    if not category:
        return False
    return categories.filter(category_code=category).exists()


def applicable_fee(provider, service=None, item=None, on_date=None):
    """The contracted price for a service or item, or ``None`` when out of scope.

    Returns a dict including ``resolution_mode`` so an adjudicator can see
    *which* mechanism priced a claim -- a price materialized into the facility
    pricelist and a price resolved by the calculation rule are otherwise
    indistinguishable after the fact (ADR-004).
    """
    mode = config_value("pce_fee_resolution_mode", "FACILITY_PRICELIST")
    result = {
        "amount": None,
        "currency": None,
        "contract": None,
        "contract_uuid": None,
        "fee_schedule": None,
        "resolution_mode": mode,
        "reason_code": None,
    }
    if not gate_enabled():
        return result

    contract = active_contract_for(provider, on_date)
    if contract is None:
        result["reason_code"] = ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT
        return result

    result["contract"] = contract.contract_number
    result["contract_uuid"] = str(contract.uuid)
    result["currency"] = contract.currency

    fee = resolve_fee(contract, service=service, item=item, on_date=on_date)
    if fee is None:
        result["reason_code"] = ClaimScopeViolation.ReasonCode.NO_FEE_MATCH
        return result

    schedule = fee.fee_schedule
    result.update(
        amount=fee.amount,
        fee_schedule=schedule.name,
        resolution_mode=schedule.resolution_mode,
    )
    return result


class ClaimScopeViolationService:
    """The queue of gate findings, and the only write side effect of a miss."""

    def __init__(self, user):
        self.user = user

    def record(self, claim, provider, verdict, contract=None, claim_service=None,
               date_of_service=None):
        """Persist a negative verdict. Never touches the claim itself.

        Idempotent via the ``pce_violation_unique`` constraint on
        (claim, claim_service, reason_code): re-running a gate over the same
        claim must not produce duplicate queue entries.
        """
        if verdict.get("empanelled"):
            return None
        reason_code = verdict.get("reason_code")
        if not reason_code:
            return None

        existing = ClaimScopeViolation.objects.filter(
            claim=claim,
            claim_service=claim_service,
            reason_code=reason_code,
        ).first()
        if existing is not None:
            return existing

        # service_code is NOT NULL and the gate's scope check is per service, so
        # fall back to the code of the service being priced. ClaimService has no
        # `service_code` field -- the code lives on the medical.Service it
        # points at -- so read it through that relation rather than trusting
        # getattr(claim_service, "service_code").
        service_code = ""
        if claim_service is not None:
            service = getattr(claim_service, "service", None)
            service_code = (getattr(service, "code", "") or "")[:6]

        violation = ClaimScopeViolation(
            claim=claim,
            claim_service=claim_service,
            provider=provider,
            contract=contract,
            service_code=service_code,
            reason_code=reason_code,
            severity=verdict.get("severity", ClaimScopeViolation.Severity.WARNING),
            details={
                "reason": verdict.get("reason", ""),
                "resolution_mode": config_value("pce_fee_resolution_mode"),
                "gate_mode": config_value("pce_gate_mode"),
            },
        )
        if date_of_service:
            violation.date_of_service = as_datetime(date_of_service)
        violation.save(user=self.user)
        return violation

    @transaction.atomic
    @register_service_signal("provider_contract_service.resolve_claim_scope_violation")
    def resolve(self, violation, resolution):
        """Mark a violation as reviewed and genuinely fixed."""
        if violation.is_resolved:
            raise ValidationError("This violation is already resolved.")
        if not resolution:
            raise ValidationError("A resolution note is required.")
        violation.is_resolved = True
        violation.reviewed_by = self.user
        violation.date_resolved = timezone.now()
        violation.resolution = resolution
        violation.save(user=self.user)
        return violation

    @transaction.atomic
    @register_service_signal("provider_contract_service.waive_claim_scope_violation")
    def waive(self, violation, reason):
        """Accept a violation without fixing its cause.

        Distinct from :meth:`resolve` on purpose: waiving says "we looked and it
        is fine after all" (a genuine misfire of the gate), resolving says "we
        corrected the underlying empanelment or pricing problem".
        """
        if violation.is_resolved:
            raise ValidationError("This violation is already resolved.")
        if not reason:
            raise ValidationError("A waiver reason is required.")
        violation.is_resolved = True
        violation.reviewed_by = self.user
        violation.date_resolved = timezone.now()
        violation.resolution = f"Waived: {reason}"
        violation.save(user=self.user)
        return violation

    @staticmethod
    def open_violations(provider=None, severity=None):
        """Unresolved queue entries, most severe first."""
        violations = ClaimScopeViolation.filter_queryset(
            ClaimScopeViolation.objects.filter(is_resolved=False)
        )
        if provider is not None:
            violations = violations.filter(provider=provider)
        if severity is not None:
            violations = violations.filter(severity=severity)
        order = {
            ClaimScopeViolation.Severity.BLOCKING: 0,
            ClaimScopeViolation.Severity.WARNING: 1,
            ClaimScopeViolation.Severity.INFO: 2,
        }
        return sorted(violations, key=lambda v: order.get(v.severity, 3))


def contract_is_priceliable(contract, on_date=None):
    """Whether ``contract`` currently has fees that can price a claim."""
    from .common import effective_fee_schedule

    return (
        effective_fee_schedule(contract, on_date) is not None
        and effective_fee_schedule(contract, on_date).status
        == ContractFeeSchedule.Status.ACTIVE
    )
