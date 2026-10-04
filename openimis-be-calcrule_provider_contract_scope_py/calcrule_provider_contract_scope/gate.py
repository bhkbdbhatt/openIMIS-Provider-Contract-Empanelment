"""The actual gate evaluation.

Everything here is a thin adapter over :mod:`provider_contract.services`. The
business rules already live there and are tested there; this module's only job
is to answer three questions the claim side asks:

* which provider treated the claim, and for which services?
* on what date should the contract have been in force?
* what did the gate conclude?

and to record a ``ClaimScopeViolation`` when the answer is negative.

Nothing in this module writes to a claim. That is the whole contract of the
module (ADR-006), so it is worth stating as an invariant: :func:`evaluate_claim`
takes a ``Claim`` and returns a summary; it never assigns to it, never calls
``save()`` on it or anything reachable from it, and never raises.
"""

import logging

from django.utils import timezone

from provider_contract.services import (
    ClaimScopeViolationService,
    applicable_fee,
    check_provider_empanelment,
    gate_enabled,
)
from provider_contract.services.common import config_value
from provider_contract.models import ClaimScopeViolation

logger = logging.getLogger(__name__)


def gate_is_enabled():
    """``pce_gate_enabled``, read once per call so tests can toggle it."""
    try:
        return bool(gate_enabled())
    except Exception:  # pragma: no cover - configuration must never break claims
        logger.exception("could not read pce_gate_enabled; treating the gate as off")
        return False


def _claim_date(claim):
    """The date the contract must have covered.

    ``date_claimed`` is the clinical date; ``date_from`` is the administrative
    one and is what claim valuation actually uses, so it is the fallback when
    the clinical date is absent. ``None`` means "today", which is what the
    services do with a missing date.
    """
    for attr in ("date_from", "date_claimed"):
        value = getattr(claim, attr, None)
        if value:
            return value
    return None


def _provider_of(claim):
    """The treating facility.

    Claim reaches the provider as ``health_facility``. A claim with no facility
    cannot be scoped, so this returns ``None`` and the gate skips it rather
    than guessing from the policy's product.
    """
    facility = getattr(claim, "health_facility", None)
    if facility is None:
        return None
    # Historical / filtered managers can hand back an id only.
    if hasattr(facility, "location_id") or hasattr(facility, "uuid"):
        return facility
    from location.models import HealthFacility

    return HealthFacility.objects.filter(id=facility).first()


def _iter_billables(claim):
    """The claim lines a contract could plausibly be scoped against."""
    services = getattr(claim, "services", None)
    if services is not None:
        try:
            for claim_service in services.all():
                service = getattr(claim_service, "service", None)
                if service is not None:
                    yield claim_service, service
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not read claim services; falling back to the claim")
    items = getattr(claim, "items", None)
    if items is not None:
        try:
            for claim_item in items.all():
                item = getattr(claim_item, "item", None)
                if item is not None:
                    yield None, item
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not read claim items")


def evaluate_claim(claim, user=None):
    """Run the scope gate over every line of ``claim``.

    :returns: a summary dict ``{"checked": int, "violations": int,
        "resolution_mode": str, "results": [...]}``, or ``None`` when the gate
        is disabled or there is nothing to scope. Safe to call twice; recording
    is idempotent on ``(claim, claim_service, reason_code)``.

    Never modifies ``claim`` and never raises: a reference module must not be
    able to fail a claim valuation.
    """
    if not gate_is_enabled():
        return None

    provider = _provider_of(claim)
    if provider is None:
        logger.debug("claim %s has no health facility; skipping the scope gate", claim.code)
        return None

    mode = config_value("pce_fee_resolution_mode", "FACILITY_PRICELIST")
    on_date = _claim_date(claim)
    results = []
    recorded = 0

    for claim_service, billable in _iter_billables(claim):
        try:
            verdict = check_provider_empanelment(
                provider, service=billable, on_date=on_date
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("scope check failed for one claim line; continuing")
            continue

        if verdict.get("empanelled"):
            results.append({"empanelled": True, "service": _code_of(billable)})
            continue

        recorded += _record(claim, provider, verdict, claim_service, on_date, user)
        results.append(
            {
                "empanelled": False,
                "service": _code_of(billable),
                "reason_code": verdict.get("reason_code"),
                "severity": verdict.get("severity"),
            }
        )

    # A claim with no priced lines still deserves one provider-level check:
    # "not empanelled at all" is the most common finding and it is per-claim,
    # not per-line.
    if not results:
        try:
            verdict = check_provider_empanelment(provider, on_date=on_date)
        except Exception:  # pragma: no cover - defensive
            logger.exception("claim-level scope check failed; giving up quietly")
            return None
        if not verdict.get("empanelled"):
            recorded += _record(claim, provider, verdict, None, on_date, user)
            results.append(
                {
                    "empanelled": False,
                    "service": None,
                    "reason_code": verdict.get("reason_code"),
                    "severity": verdict.get("severity"),
                }
            )

    return {
        "checked": len(results),
        "violations": recorded,
        "resolution_mode": mode,
        "results": results,
    }


def resolve_fee_for(claim, service=None, item=None, user=None):
    """The contracted price for one line, for ``CONTRACT_CALCULE`` deployments.

    Separate from :func:`evaluate_claim` on purpose: pricing is an input to
    valuation, flagging is an output of it. In ``CONTRACT_CALCULE`` mode a
    calcrule is expected to hand a price back, so this is the entry point for
    that. Returns ``None`` when the gate is off, so the caller falls back to the
    facility pricelist.

    .. note::
       **Nothing in openIMIS calls this yet.** The claim module prices from
       ``HealthFacility.services_pricelist`` and never consults a calculation
       rule, so ``CONTRACT_CALCULE`` mode still needs a one-line change in claim
       valuation to actually route pricing through here. That edit is left to
       the deploying scheme rather than made from a reference module: it changes
       the price of every claim, which is not a change a reference module should
       make on its own. Until then ``applicable_fee`` over the GraphQL API is
       how an adjudicator sees the contracted fee.
    """
    if not gate_is_enabled():
        return None
    provider = _provider_of(claim)
    if provider is None:
        return None
    result = applicable_fee(
        provider, service=service, item=item, on_date=_claim_date(claim)
    )
    if result.get("reason_code") == ClaimScopeViolation.ReasonCode.NO_FEE_MATCH:
        logger.info(
            "no contracted fee for claim %s; the caller keeps the pricelist price",
            claim.code,
        )
    return result


def _code_of(billable):
    return getattr(billable, "code", None)


def _record(claim, provider, verdict, claim_service, on_date, user):
    """Persist one finding. Returns 1 when a new row was created, else 0.

    Failures are logged and swallowed: losing a flag is regrettable, failing a
    claim valuation over it is worse.
    """
    if user is None:
        from core.models import User

        user = User.objects.order_by("username", "id").first()
    if user is None:
        logger.warning(
            "no user available to attribute a scope violation to; skipping"
        )
        return 0
    try:
        ClaimScopeViolationService(user).record(
            claim,
            provider,
            verdict,
            claim_service=claim_service,
            date_of_service=_as_datetime(on_date),
        )
        return 1
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "could not record a claim scope violation for claim %s", claim.code
        )
        return 0


def _as_datetime(value):
    if value is None:
        return None
    if hasattr(value, "hour"):
        try:
            return timezone.make_aware(value)
        except Exception:
            return value
    return timezone.make_aware(
        timezone.datetime(
            value.year, value.month, value.day, tzinfo=None
        )
    )
