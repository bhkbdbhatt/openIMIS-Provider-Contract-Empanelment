"""Contract lifecycle: create, negotiate, sign, activate, amend, renew, terminate.

The lifecycle is a small state machine::

    DRAFT -> NEGOTIATION -> SIGNED -> ACTIVE -> RENEWED
       |            |           |        |
       +------------+-----------+--------+--> SUSPENDED -> ACTIVE
       |            |           |        |
       +------------+-----------+--------+--> TERMINATED
                    |           |
                    +-----------+--> EXPIRED (derived from date_end)

Two rules are enforced here in services rather than with database constraints,
and both are deliberate (ADR-002):

* **one ACTIVE contract per facility** in ``FACILITY_PRICELIST`` mode;
* **one open version per ``contract_number``**.

They cannot be expressed as partial unique indexes because openIMIS must also
run on MSSQL, and MSSQL has no partial indexes. The weaker-but-portable
alternative is ``transaction.atomic()`` plus ``select_for_update()`` on the
provider and contract rows, which is what this module does.

Amendment is never an in-place update of a dated row. :meth:`amend` closes the
current version and opens a new one through ``ValidityMixin.replace_object``, so
historical claim pricing stays reproducible.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from location.models import HealthFacility

from core.signals import register_service_signal

from provider_contract.models import ContractFeeSchedule, ProviderContract

from .common import as_date, config_value


class ProviderContractService:
    def __init__(self, user):
        self.user = user

    # ------------------------------------------------------------------ reads

    def get_contract(self, contract_uuid, for_update=False):
        """Fetch a live contract, optionally locking the row."""
        queryset = ProviderContract.filter_queryset(ProviderContract.objects.all())
        if for_update:
            queryset = queryset.select_for_update()
        contract = queryset.filter(uuid=contract_uuid).first()
        if contract is None:
            raise ValidationError(f"Contract {contract_uuid} not found or not visible.")
        return contract

    def _lock_provider(self, provider_id):
        """Lock the health facility row to serialise concurrent activations.

        The lock has to be taken on ``HealthFacility`` rather than on the
        contract, because the invariant being protected ("only one ACTIVE
        contract for this facility") is shared across contract rows.
        """
        return HealthFacility.objects.select_for_update().filter(pk=provider_id).first()

    # ------------------------------------------------------------- validation

    def _assert_dates_ordered(self, data):
        start = as_date(data.get("date_start"))
        end = as_date(data.get("date_end"))
        if start and end and end < start:
            raise ValidationError("date_end cannot precede date_start.")

    def _assert_single_active(self, provider, exclude=None):
        """Reject a second ACTIVE contract for the same facility.

        Only enforced in ``FACILITY_PRICELIST`` mode: ``CONTRACT_CALCULE``
        resolves fees per contract and explicitly supports concurrent contracts.
        """
        if config_value("pce_fee_resolution_mode") != "FACILITY_PRICELIST":
            return
        others = ProviderContract.objects.filter(
            provider=provider, status=ProviderContract.Status.ACTIVE
        )
        if exclude:
            others = others.exclude(uuid=exclude)
        if others.exists():
            raise ValidationError(
                "This facility already has an ACTIVE contract "
                f"({others.first().contract_number}). Terminate or renew it first, "
                "or switch pce_fee_resolution_mode to CONTRACT_CALCULE."
            )

    def _assert_single_open_version(self, contract_number, exclude=None):
        """Reject a second non-closed version of the same contract number."""
        queryset = ProviderContract.objects.filter(
            contract_number=contract_number, date_valid_to__isnull=True
        )
        if exclude:
            queryset = queryset.exclude(uuid=exclude)
        if queryset.exists():
            raise ValidationError(
                f"An open version of contract {contract_number} already exists. "
                "Amend it instead of creating a second one."
            )

    # ------------------------------------------------------------ transitions

    @transaction.atomic
    @register_service_signal("provider_contract_service.create_provider_contract")
    def create(self, data):
        provider_id = data.get("provider_id")
        provider = self._resolve_provider(provider_id)
        self._assert_dates_ordered(data)

        contract_number = data["contract_number"]
        self._assert_single_open_version(contract_number)

        contract = ProviderContract(
            provider=provider,
            contract_number=contract_number,
            currency=data.get("currency", "USD"),
            date_start=as_date(data["date_start"]),
            date_end=as_date(data["date_end"]),
            date_renewal=as_date(data.get("date_renewal")),
            notice_period_days=data.get(
                "notice_period_days", config_value("pce_default_renewal_window_days", 60)
            ),
            auto_renew=data.get("auto_renew", False),
            notes=data.get("notes"),
            status=ProviderContract.Status.DRAFT,
            version_no=1,
            empanelment_process_id=data.get("empanelment_process_id"),
        )
        contract.save(user=self.user)
        return contract

    def _resolve_provider(self, provider_id):
        """Resolve a health facility from its ``uuid`` string.

        ``HealthFacility.uuid`` is a ``CharField(36)``, which is why the GraphQL
        argument is ``String!`` rather than ``UUID!``.
        """
        if provider_id is None:
            raise ValidationError("provider_id is required.")
        provider = (
            HealthFacility.objects.filter(uuid=str(provider_id))
            .filter(validity_to__isnull=True)
            .first()
        )
        if provider is None:
            raise ValidationError(f"Health facility {provider_id} not found.")
        return provider

    @transaction.atomic
    @register_service_signal("provider_contract_service.update_provider_contract")
    def update(self, contract, data):
        if contract.status not in (
            ProviderContract.Status.DRAFT,
            ProviderContract.Status.NEGOTIATION,
        ):
            raise ValidationError(
                f"A {contract.get_status_display()} contract cannot be edited. "
                "Use amend_provider_contract to change an agreed contract."
            )
        for field in ("date_start", "date_end", "date_renewal", "notes"):
            if field in data:
                setattr(contract, field, as_date(data[field]) if field.startswith("date_") else data[field])
        if "currency" in data:
            contract.currency = data["currency"]
        self._assert_dates_ordered(
            {"date_start": contract.date_start, "date_end": contract.date_end}
        )
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.submit_provider_contract")
    def submit(self, contract):
        """DRAFT -> NEGOTIATION."""
        self._assert_status(contract, ProviderContract.Status.DRAFT)
        contract.status = ProviderContract.Status.NEGOTIATION
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.approve_provider_contract")
    def approve(self, contract, date_signed=None):
        """NEGOTIATION -> SIGNED, stamping the signature date."""
        self._assert_status(contract, ProviderContract.Status.NEGOTIATION)
        contract.status = ProviderContract.Status.SIGNED
        contract.date_signed = as_date(date_signed) or timezone.now().date()
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.activate_provider_contract")
    def activate(self, contract):
        """SIGNED -> ACTIVE.

        Refuses to activate without at least one ACTIVE fee schedule: a contract
        with no prices would either fall back to the facility's default pricelist
        (pricing services the contract does not cover) or price nothing at all.
        """
        self._assert_status(contract, ProviderContract.Status.SIGNED)
        if not ContractFeeSchedule.objects.filter(
            contract=contract, status=ContractFeeSchedule.Status.ACTIVE
        ).exists():
            raise ValidationError(
                "Cannot activate a contract without an ACTIVE fee schedule."
            )
        self._lock_provider(contract.provider_id)
        self._assert_single_active(contract.provider, exclude=contract.uuid)
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.suspend_provider_contract")
    def suspend(self, contract, reason=None):
        """ACTIVE -> SUSPENDED. Fees stop resolving; the contract is retained."""
        self._assert_status(contract, ProviderContract.Status.ACTIVE)
        contract.status = ProviderContract.Status.SUSPENDED
        contract.notes = reason or contract.notes
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.resume_provider_contract")
    def resume(self, contract):
        """SUSPENDED -> ACTIVE, re-checking the single-active rule on the way."""
        self._assert_status(contract, ProviderContract.Status.SUSPENDED)
        self._lock_provider(contract.provider_id)
        self._assert_single_active(contract.provider, exclude=contract.uuid)
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.amend_provider_contract")
    def amend(self, contract, data):
        """Open a new version of an agreed contract.

        The current version is closed and a successor created by
        ``replace_object``; the successor inherits the contract status and
        carries an incremented ``version_no``. Amounts already priced against the
        old version stay reproducible.
        """
        if contract.status in (
            ProviderContract.Status.TERMINATED,
            ProviderContract.Status.EXPIRED,
        ):
            raise ValidationError(
                f"A {contract.get_status_display()} contract cannot be amended."
            )
        changes = {}
        for field in ("date_start", "date_end", "date_renewal", "currency", "notes"):
            if field in data and data[field] is not None:
                changes[field] = (
                    as_date(data[field]) if field.startswith("date_") else data[field]
                )
        self._assert_dates_ordered(
            {
                "date_start": changes.get("date_start", contract.date_start),
                "date_end": changes.get("date_end", contract.date_end),
            }
        )
        changes["version_no"] = contract.version_no + 1
        changes["status"] = ProviderContract.Status.DRAFT

        contract.replace_object(changes, username=self.user.username)
        # replace_object() returns None. It writes the successor's id onto the
        # row it superseded, so that is where the new version has to be read
        # back from -- querying by replacement_uuid=contract.id looks right but
        # inverts the direction of the link and finds nothing.
        contract.refresh_from_db()
        amended = ProviderContract.objects.get(uuid=contract.replacement_uuid)
        return amended

    @transaction.atomic
    @register_service_signal("provider_contract_service.renew_provider_contract")
    def renew(self, contract, date_start=None, date_end=None, auto_renew=None):
        """Renew an ACTIVE or EXPIRED contract into a new version.

        The old version is marked RENEWED rather than deleted so claims priced
        against it remain explainable.
        """
        if contract.status not in (
            ProviderContract.Status.ACTIVE,
            ProviderContract.Status.EXPIRED,
        ):
            raise ValidationError(
                f"A {contract.get_status_display()} contract cannot be renewed."
            )
        self._assert_single_open_version(contract.contract_number, exclude=contract.uuid)

        new_start = as_date(date_start) or (contract.date_end + timedelta(days=1))
        new_end = as_date(date_end)
        if new_end is None:
            raise ValidationError("date_end is required to renew a contract.")
        if new_end < new_start:
            raise ValidationError("date_end cannot precede date_start.")

        contract.status = ProviderContract.Status.RENEWED
        contract.save(user=self.user)

        changes = {
            "status": ProviderContract.Status.DRAFT,
            "date_start": new_start,
            "date_end": new_end,
            "version_no": contract.version_no + 1,
            "date_signed": None,
            "date_terminated": None,
            "termination_reason": None,
        }
        if auto_renew is not None:
            changes["auto_renew"] = auto_renew
        contract.replace_object(changes, username=self.user.username)
        # See amend(): replace_object returns None and back-links the successor.
        contract.refresh_from_db()
        return ProviderContract.objects.get(uuid=contract.replacement_uuid)

    @transaction.atomic
    @register_service_signal("provider_contract_service.terminate_provider_contract")
    def terminate(self, contract, reason):
        """Close a contract early and stop it pricing.

        The version is closed (``date_valid_to``) rather than deleted: claims
        already priced against it must remain reproducible, and the gate needs to
        be able to explain them.
        """
        if not reason:
            raise ValidationError("A termination reason is required.")
        if contract.status in (
            ProviderContract.Status.TERMINATED,
            ProviderContract.Status.EXPIRED,
        ):
            raise ValidationError(
                f"A {contract.get_status_display()} contract cannot be terminated."
            )
        now = timezone.now()
        contract.status = ProviderContract.Status.TERMINATED
        contract.date_terminated = now.date()
        contract.termination_reason = reason
        contract.date_valid_to = now
        contract.save(user=self.user)
        return contract

    @transaction.atomic
    @register_service_signal("provider_contract_service.delete_provider_contract")
    def delete(self, contract):
        """Soft-delete a contract that has never been agreed."""
        if contract.status != ProviderContract.Status.DRAFT:
            raise ValidationError(
                "Only a DRAFT contract can be deleted. Terminate an agreed one so "
                "its pricing history is preserved."
            )
        contract.delete(user=self.user)
        return contract

    # ----------------------------------------------------------------- helper

    @staticmethod
    def _assert_status(contract, *allowed):
        if contract.status not in allowed:
            names = ", ".join(
                c.label for c in ProviderContract.Status if c.value in allowed
            )
            raise ValidationError(
                f"Contract {contract.contract_number} is "
                f"{contract.get_status_display()}; expected one of: {names}."
            )


def contracts_expiring_within(days=None):
    """ACTIVE contracts whose renewal window has opened.

    Exposed for the scheduled renewal task and for a ``contracts`` query that
    schemes use to drive their renewal queue.
    """
    days = days if days is not None else config_value("pce_default_renewal_window_days", 60)
    today = timezone.now().date()
    horizon = today + timedelta(days=days)
    return ProviderContract.objects.filter(
        status=ProviderContract.Status.ACTIVE,
        date_end__gte=today,
        date_end__lte=horizon,
    )


def contracts_past_end_date(reference=None):
    """ACTIVE contracts whose ``date_end`` has already passed.

    Read-only on purpose. ``contracts_covering`` already excludes these by date,
    so correctness never depends on a sweeper having run; this exists so reports
    and the gate can distinguish "expired last month" from "never active". Use
    ``expire_contracts`` to actually move them to EXPIRED.
    """
    reference = as_date(reference) or timezone.now().date()
    return ProviderContract.objects.filter(
        status=ProviderContract.Status.ACTIVE, date_end__lt=reference
    )


@transaction.atomic
@register_service_signal("provider_contract_service.expire_provider_contracts")
def expire_contracts(user, reference=None):
    """Move ACTIVE contracts past their ``date_end`` to EXPIRED.

    Idempotent, and deliberately a separate operation from the read path.
    """
    candidates = contracts_past_end_date(reference)
    count = 0
    for contract in candidates:
        contract.status = ProviderContract.Status.EXPIRED
        contract.save(user=user)
        count += 1
    return count
