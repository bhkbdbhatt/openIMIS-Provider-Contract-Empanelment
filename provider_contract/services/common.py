"""Shared helpers for the provider_contract service layer.

Two concerns live here because getting either wrong is a correctness bug rather
than a style issue: reading module configuration, and deciding which contract
version governs a given provider on a given date.

The version question is the subtle one. A contract is a chain of versions
(``date_valid_from`` / ``date_valid_to``), and a claim must be priced against
the version that was in force when the service was rendered -- otherwise a
contract amended today would retroactively change the price of a claim from
last year. So validity is evaluated at the *date of service*, not at "now".
"""

from datetime import date, datetime

from django.apps import apps as django_apps
from django.db.models import Q
from django.utils import timezone

from provider_contract.models import (
    ContractFeeItem,
    ContractFeeSchedule,
    ProviderContract,
)


def get_config():
    """The live AppConfig, so overridden module configuration is respected.

    Configuration is loaded once in ``AppConfig.ready()`` onto class attributes.
    Reading it through the config object rather than re-importing ``DEFAULT_CFG``
    is what makes a ``ModuleConfiguration`` row take effect without a redeploy.
    """
    return django_apps.get_app_config("provider_contract")


def config_value(name, fallback=None):
    return getattr(get_config(), name, fallback)


def as_date(value):
    """Coerce a date, datetime or ISO string into a ``date``.

    GraphQL hands over ISO strings, forms hand over ``date`` objects and the
    backfill hands over ``date``. Comparing a ``date`` to a ``datetime`` raises
    in Python 3, so normalise once at the boundary rather than at each call site.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def as_datetime(value):
    """Coerce to an aware ``datetime``. openIMIS runs with ``USE_TZ``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    if isinstance(value, date):
        return timezone.make_aware(datetime.combine(value, datetime.min.time()))
    parsed = datetime.fromisoformat(str(value))
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def valid_at(reference):
    """``Q`` selecting rows whose version is in force at ``reference``.

    ``date_valid_from`` is inclusive and ``date_valid_to`` exclusive, matching
    the convention ``ProviderContractBusinessModel.filter_validity`` uses.
    """
    return Q(date_valid_from__lte=reference) & (
        Q(date_valid_to__isnull=True) | Q(date_valid_to__gt=reference)
    )


def contracts_covering(provider, on_date=None):
    """Contract versions of ``provider`` in force on ``on_date``.

    Two independent conditions, both required:

    * the version was in force at that date (``date_valid_from`` /
      ``date_valid_to``), so an amendment does not rewrite history; and
    * the contract's business period covers it. ``date_end`` is **inclusive** --
      a contract ending 2026-12-31 prices services rendered on 2026-12-31 --
      whereas ``date_valid_to`` is exclusive.

    Ordered newest version first so the caller can prefer it.
    """
    on_date = as_date(on_date) or timezone.now().date()
    reference = as_datetime(on_date)
    return (
        ProviderContract.objects.filter(
            provider=provider,
            date_start__lte=on_date,
            date_end__gte=on_date,
        )
        .filter(valid_at(reference))
        .order_by("-version_no")
    )


def current_active_contract(provider, on_date=None):
    """The ACTIVE contract version governing ``provider``, or ``None``.

    Used for present-tense questions (is this provider empanelled right now).
    For a claim, prefer :func:`contracts_covering` at the date of service.
    """
    on_date = as_date(on_date) or timezone.now().date()
    return (
        contracts_covering(provider, on_date)
        .filter(status=ProviderContract.Status.ACTIVE)
        .first()
    )


def active_contract_for(provider, on_date=None):
    """First ACTIVE contract covering ``on_date``.

    Kept as the general-purpose lookup; callers that must insist on exactly one
    active contract (the ``FACILITY_PRICELIST`` mode) should check for more.
    """
    return (
        contracts_covering(provider, on_date)
        .filter(status=ProviderContract.Status.ACTIVE)
        .first()
    )


def effective_fee_schedule(contract, on_date=None):
    """The ACTIVE fee schedule of ``contract`` in force on ``on_date``."""
    on_date = as_date(on_date) or timezone.now().date()
    return (
        ContractFeeSchedule.objects.filter(
            contract=contract,
            status=ContractFeeSchedule.Status.ACTIVE,
        )
        .filter(valid_at(as_datetime(on_date)))
        .first()
    )


def resolve_fee(contract, service=None, item=None, on_date=None):
    """The contract fee for a service or item, or ``None`` when out of scope.

    This is the read path behind ``applicable_fee`` and behind the deferred
    ``calcrule_provider_contract_scope`` calculation rule. It never mutates and
    never raises on a miss, because its caller is a claim being priced.
    """
    schedule = effective_fee_schedule(contract, on_date)
    if schedule is None:
        return None
    fees = ContractFeeItem.objects.filter(fee_schedule=schedule, is_included=True)
    if service is not None:
        fees = fees.filter(service=service)
    if item is not None:
        fees = fees.filter(item=item)
    return fees.first()
