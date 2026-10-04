"""Fee schedules, fee rows, and materialization into openIMIS pricelists.

Two resolution modes (ADR-004):

``FACILITY_PRICELIST`` (default)
    On activation the schedule's fee rows are projected into a dedicated
    ``medical_pricelist.ServicesPricelist`` / ``ItemsPricelist``, and the
    facility's ``services_pricelist`` / ``items_pricelist`` is pointed at it.
    Claim valuation then works completely unchanged. Requires exactly one
    ACTIVE contract per facility, because a facility has a single pricelist slot.

``CONTRACT_CALCULE``
    ``HealthFacility.services_pricelist`` is left untouched and the deferred
    ``calcrule_provider_contract_scope`` resolves ``ContractFeeItem`` directly.
    Supports any number of concurrent contracts per facility.

Materialization is the reason fee rows carry exactly one of ``service`` or
``item`` (the ``pce_fee_item_single_target`` constraint): a row that pointed at
both could not be projected onto two different pricelists unambiguously.

Amounts use ``DecimalField(max_digits=18, decimal_places=2)`` precisely to match
``ServicesPricelistDetail.price_overrule``, so projecting loses no precision.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from medical.models import Item, Service
from medical_pricelist.models import (
    ItemsPricelist,
    ItemsPricelistDetail,
    ServicesPricelist,
    ServicesPricelistDetail,
)

from core.signals import register_service_signal

from provider_contract.models import (
    ContractFeeItem,
    ContractFeeSchedule,
    ProviderContract,
)


class ContractFeeService:
    def __init__(self, user):
        self.user = user

    # ------------------------------------------------------------- schedules

    @transaction.atomic
    @register_service_signal("provider_contract_service.create_contract_fee_schedule")
    def create_schedule(self, contract, data):
        schedule = ContractFeeSchedule(
            contract=contract,
            name=data["name"],
            fee_type=data.get("fee_type", ContractFeeSchedule.FeeType.FFS),
            currency=data.get("currency", contract.currency),
            resolution_mode=data.get(
                "resolution_mode", ContractFeeSchedule.ResolutionMode.FACILITY_PRICELIST
            ),
            status=ContractFeeSchedule.Status.DRAFT,
        )
        schedule.save(user=self.user)
        return schedule

    @transaction.atomic
    @register_service_signal("provider_contract_service.activate_contract_fee_schedule")
    def activate_schedule(self, schedule, activate_contract=False):
        """DRAFT -> ACTIVE, materializing into a pricelist where required.

        ``activate_contract`` is offered here rather than making the caller chain
        two mutations, because in ``FACILITY_PRICELIST`` mode activating a
        contract with no ACTIVE schedule is refused, and a caller almost always
        wants both.
        """
        if schedule.status != ContractFeeSchedule.Status.DRAFT:
            raise ValidationError(
                f"Schedule {schedule.name} is {schedule.get_status_display()}; "
                "only a DRAFT schedule can be activated."
            )
        if not schedule.fee_items.exists():
            raise ValidationError("Cannot activate an empty fee schedule.")

        supersede_others = ContractFeeSchedule.objects.filter(
            contract=schedule.contract,
            status=ContractFeeSchedule.Status.ACTIVE,
        ).exclude(uuid=schedule.uuid)
        for other in supersede_others:
            other.status = ContractFeeSchedule.Status.SUPERSEDED
            other.save(user=self.user)

        if schedule.resolution_mode == ContractFeeSchedule.ResolutionMode.FACILITY_PRICELIST:
            self._materialize(schedule)

        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)

        if activate_contract:
            # Imported here because contract_lifecycle does not import this
            # module, so a module-level import would be circular.
            from .contract_lifecycle import ProviderContractService

            if schedule.contract.status == ProviderContract.Status.SIGNED:
                ProviderContractService(self.user).activate(schedule.contract)

        return schedule

    def _materialize(self, schedule):
        """Project the schedule's fee rows onto openIMIS pricelists.

        The pricelists are created with ``validity_to=None`` so they stay live;
        superseding a schedule supersedes the previous pricelist rather than
        mutating prices under claims that were already valued against them.
        """
        facility = schedule.contract.provider
        location = facility.location
        audit_user_id = self._audit_user_id()

        services_pl = self._supersede_pricelist(
            ServicesPricelist, schedule.contract.provider.services_pricelist,
            f"PCE {schedule.contract.contract_number} v{schedule.contract.version_no}",
            location, audit_user_id,
        )
        items_pl = self._supersede_pricelist(
            ItemsPricelist, schedule.contract.provider.items_pricelist,
            f"PCE {schedule.contract.contract_number} v{schedule.contract.version_no}",
            location, audit_user_id,
        )

        for fee in schedule.fee_items.filter(is_included=True):
            if fee.service_id is not None:
                ServicesPricelistDetail.objects.create(
                    services_pricelist=services_pl,
                    service_id=fee.service_id,
                    price_overrule=fee.amount,
                    audit_user_id=audit_user_id,
                )
            elif fee.item_id is not None:
                ItemsPricelistDetail.objects.create(
                    items_pricelist=items_pl,
                    item_id=fee.item_id,
                    price_overrule=fee.amount,
                    audit_user_id=audit_user_id,
                )

        schedule.services_pricelist = services_pl
        schedule.items_pricelist = items_pl
        schedule.save(user=self.user)

        facility.services_pricelist = services_pl
        facility.items_pricelist = items_pl
        facility.save()
        return services_pl, items_pl

    @staticmethod
    def _supersede_pricelist(model, current, name, location, audit_user_id):
        """Close the facility's current pricelist and open a fresh one.

        A new pricelist rather than an update: the previous one may already have
        been used to value claims, and rewriting its prices would change those
        valuations retroactively.
        """
        if current is not None and current.validity_to is None:
            current.validity_to = timezone.now()
            current.save()
        return model.objects.create(
            name=name[:100],
            location=location,
            audit_user_id=audit_user_id,
        )

    # ``medical_pricelist`` models are legacy ``VersionedModel`` rows that want
    # an integer ``audit_user_id`` rather than the ``core.User`` foreign key used
    # by this module. openIMIS marks system writes with -1, which is also what
    # the location and medical_pricelist fixtures use.
    AUDIT_USER_ID = -1

    def _audit_user_id(self):
        return self.AUDIT_USER_ID

    # -------------------------------------------------------------- fee rows

    @transaction.atomic
    @register_service_signal("provider_contract_service.create_contract_fee_item")
    def upsert_fee_item(self, schedule, service=None, item=None, amount=None,
                        quantity=1, overrule_max_amount=None):
        """Create or update one fee row.

        ``pce_fee_item_single_target`` requires exactly one of ``service`` or
        ``item``; validated here so the caller gets a readable message instead of
        an ``IntegrityError``.
        """
        if (service is None) == (item is None):
            raise ValidationError(
                "A fee row must target exactly one of a service or an item."
            )
        if amount is None:
            raise ValidationError("amount is required.")
        amount = Decimal(str(amount))
        if amount < 0:
            raise ValidationError("amount cannot be negative.")

        fees = schedule.fee_items.all()
        if service is not None:
            fees = fees.filter(service=service)
        if item is not None:
            fees = fees.filter(item=item)
        fee = fees.first()

        if fee is None:
            fee = ContractFeeItem(
                fee_schedule=schedule,
                service=service,
                item=item,
                amount=amount,
                quantity=quantity,
                overrule_max_amount=overrule_max_amount,
            )
        else:
            fee.amount = amount
            fee.quantity = quantity
            if overrule_max_amount is not None:
                fee.overrule_max_amount = overrule_max_amount
        fee.save(user=self.user)
        return fee

    @transaction.atomic
    @register_service_signal("provider_contract_service.delete_contract_fee_item")
    def delete_fee_item(self, fee):
        """Soft-delete a fee row, refusing once its schedule is live.

        An ACTIVE schedule is the thing claims are priced against; editing it
        in place would retroactively change those prices. Amend the contract and
        activate a new schedule instead.
        """
        if fee.fee_schedule.status == ContractFeeSchedule.Status.ACTIVE:
            raise ValidationError(
                "This fee row belongs to an ACTIVE schedule and cannot be removed. "
                "Amend the contract and activate a replacement schedule."
            )
        fee.delete(user=self.user)
        return fee

    @transaction.atomic
    @register_service_signal("provider_contract_service.bulk_update_fees")
    def bulk_update_fees(self, schedule, rows):
        """Apply many fee rows at once, reporting per-row outcomes.

        Import-style behaviour on purpose: one bad row in a 400-row spreadsheet
        must not discard the other 399, because the operator has to fix and
        re-upload only what failed. Returns ``(succeeded, errors)``.
        """
        if schedule.status == ContractFeeSchedule.Status.ACTIVE:
            raise ValidationError(
                "Cannot bulk-edit an ACTIVE schedule; activate a replacement instead."
            )
        succeeded = 0
        errors = []
        for index, row in enumerate(rows, start=1):
            try:
                with transaction.atomic():
                    service = self._resolve_service(row.get("service_code"))
                    item = self._resolve_item(row.get("item_code"))
                    self.upsert_fee_item(
                        schedule,
                        service=service,
                        item=item,
                        amount=row.get("amount"),
                        quantity=row.get("quantity", 1),
                    )
                succeeded += 1
            except (ValidationError, ValueError, ArithmeticError) as exc:
                errors.append({"row": index, "error": str(exc)})
        return succeeded, errors

    @staticmethod
    def _resolve_service(code):
        if not code:
            return None
        service = Service.objects.filter(code=code, validity_to__isnull=True).first()
        if service is None:
            raise ValidationError(f"Unknown service {code!r}.")
        return service

    @staticmethod
    def _resolve_item(code):
        if not code:
            return None
        item = Item.objects.filter(code=code, validity_to__isnull=True).first()
        if item is None:
            raise ValidationError(f"Unknown item {code!r}.")
        return item


def fee_for(contract, service=None, item=None, on_date=None):
    """Read-path fee lookup. See ``services.common.resolve_fee``."""
    from .common import resolve_fee

    return resolve_fee(contract, service=service, item=item, on_date=on_date)
