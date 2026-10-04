"""GraphQL mutation layer for provider empanelment and contracts.

Every mutation here is a thin shell around a service in
:mod:`provider_contract.services`. No business rule -- and no state
transition -- lives in this file; if a transition cannot be expressed by
calling one service method, it belongs in the service layer where it can be
tested without a GraphQL request.

How a caller learns that a mutation failed, which is the part that is easy to
get wrong:

``OpenIMISMutation`` replies to the request immediately with an
``internalId`` and then does the work. Whatever happens, the *response body*
still looks like a success; the outcome is on the ``MutationLog`` row that
``internalId`` points at, and the caller polls it. It also wraps
``async_mutate`` in a bare ``except Exception``, so **both** of these end up the
same way -- log status ``ERROR`` with a message:

* returning a non-empty error list, and
* raising, including ``PermissionDenied``.

So the error list returned here is not an "authorisation channel"; it is
purely a rendering of *why* a business rule rejected the request, preserved in
the log so an operator can read it. The distinction this file does maintain is
that authorisation is checked in the decorator's preamble, outside the
``try``, so a missing right is never mistaken for a business rejection in the
logs, and a business ``ValidationError`` never escapes as a 500.

**``async_mutate`` must return ``None`` on success.** The base class reads the
return value as ``error_messages``; ``if not error_messages: mark_as_successful()
else: mark_as_failed(json.dumps(error_messages))``. A *dict* of useful results
is truthy, so returning one marks the mutation **failed** and files your
payload under ``error``. Returning ``None`` is the only success signal there
is; clients read the created rows back over the query API, or via
``client_mutation_label``.
"""

import functools
import logging

import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _
from graphene import InputObjectType

from core.schema import OpenIMISMutation, ParsedJSONString

from ..apps import ProviderContractConfig
from ..models import (
    ClaimScopeViolation,
    ContractFeeItem,
    ContractFeeSchedule,
    EmpanelmentDocumentType,
    EmpanelmentProcess,
    EmpanelmentProcessDocument,
    EmpanelmentStageTransition,
    EmpanelmentWorkflow,
    ProviderContract,
)
from ..services import (
    ClaimScopeViolationService,
    ContractFeeService,
    EmpanelmentService,
    ProviderContractService,
)

logger = logging.getLogger(__name__)


def pce_mutation(perms, failure_key, label=None):
    """Decorate ``async_mutate`` with auth, authorisation and error handling.

    :param perms: name of the ``ProviderContractConfig`` attribute holding the
        right ids this mutation requires.
    :param failure_key: gettext key for the message recorded when the service
        raises. It is formatted with ``{"label": ...}``.
    :param label: optional callable ``data -> str`` producing the value
        interpolated into the message, e.g. the contract number.

    Authentication and authorisation run before the ``try`` so that a missing
    right is never logged as a business rejection. Everything after that is
    converted to the standard error list, which ``OpenIMISMutation`` writes to
    the ``MutationLog``.
    """

    def decorate(func):
        label_of = label or (lambda data: "")

        @functools.wraps(func)
        def wrapper(cls, user, **data):
            # Outside the try: these must propagate, not become an error list.
            if type(user) is AnonymousUser or not getattr(user, "id", None):
                raise ValidationError(_("mutation.authentication_required"))
            if not user.has_perms(getattr(ProviderContractConfig, perms)):
                raise PermissionDenied(_("unauthorized"))
            try:
                return func(cls, user, **data)
            except Exception as exc:
                logger.warning(
                    "%s failed for user %s: %s",
                    func.__qualname__,
                    getattr(user, "id", "?"),
                    exc,
                    exc_info=True,
                )
                return [
                    {
                        "message": _(failure_key) % {"label": label_of(data)},
                        "detail": str(exc),
                    }
                ]

        return wrapper

    return decorate


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class CreateEmpanelmentProcessInput(OpenIMISMutation.Input):
    provider_id = graphene.String(
        required=True, description="HealthFacility.uuid (CharField(36))."
    )
    workflow_id = graphene.String(
        required=True, description="EmpanelmentWorkflow.uuid."
    )
    reference_no = graphene.String(required=True, max_length=100)
    valid_until = graphene.Date(required=False)
    json_ext = ParsedJSONString(required=False)


class UpdateEmpanelmentProcessInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="EmpanelmentProcess.uuid.")
    reference_no = graphene.String(required=False, max_length=100)
    valid_until = graphene.Date(required=False)
    json_ext = ParsedJSONString(required=False)


class DecideEmpanelmentProcessInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="EmpanelmentProcess.uuid.")
    approved = graphene.Boolean(required=True)
    reason = graphene.String(required=False, max_length=255)


class AdvanceEmpanelmentProcessInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="EmpanelmentProcess.uuid.")
    outcome = graphene.String(
        required=False, description="PASSED or FAILED. Defaults to PASSED."
    )
    comment = graphene.String(required=False, max_length=255)


class WithdrawEmpanelmentProcessInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="EmpanelmentProcess.uuid.")
    reason = graphene.String(required=False, max_length=255)


class AddEmpanelmentDocumentInput(OpenIMISMutation.Input):
    process_id = graphene.String(required=True, description="EmpanelmentProcess.uuid.")
    document_type_id = graphene.String(
        required=True, description="EmpanelmentDocumentType.uuid."
    )
    file_reference = graphene.String(required=False, max_length=255)
    date_expires = graphene.Date(required=False)
    json_ext = ParsedJSONString(required=False)


class ReviewEmpanelmentDocumentInput(OpenIMISMutation.Input):
    id = graphene.String(
        required=True, description="EmpanelmentProcessDocument.uuid."
    )
    note = graphene.String(required=False, max_length=255)


class CreateProviderContractInput(OpenIMISMutation.Input):
    provider_id = graphene.String(required=True, description="HealthFacility.uuid.")
    empanelment_process_id = graphene.String(required=False)
    contract_number = graphene.String(required=True, max_length=30)
    date_start = graphene.Date(required=True)
    date_end = graphene.Date(required=True)
    date_renewal = graphene.Date(required=False)
    currency = graphene.String(required=False, max_length=3)
    notice_period_days = graphene.Int(required=False)
    auto_renew = graphene.Boolean(required=False)
    notes = graphene.String(required=False, max_length=255)
    json_ext = ParsedJSONString(required=False)


class UpdateProviderContractInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="ProviderContract.uuid.")
    date_renewal = graphene.Date(required=False)
    notice_period_days = graphene.Int(required=False)
    auto_renew = graphene.Boolean(required=False)
    notes = graphene.String(required=False, max_length=255)
    currency = graphene.String(required=False, max_length=3)
    json_ext = ParsedJSONString(required=False)


class AmendProviderContractInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="ProviderContract.uuid.")
    date_start = graphene.Date(required=False)
    date_end = graphene.Date(required=False)
    date_renewal = graphene.Date(required=False)
    currency = graphene.String(required=False, max_length=3)
    notes = graphene.String(required=False, max_length=255)


class RenewProviderContractInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="ProviderContract.uuid.")
    date_start = graphene.Date(required=False)
    date_end = graphene.Date(required=True)
    auto_renew = graphene.Boolean(required=False)


class ContractReasonInput(OpenIMISMutation.Input):
    """Shared shape for terminate / suspend / waive: an id and a reason."""

    id = graphene.String(required=True, description="uuid of the target row.")
    reason = graphene.String(required=True, max_length=255)


class CreateContractFeeScheduleInput(OpenIMISMutation.Input):
    contract_id = graphene.String(required=True, description="ProviderContract.uuid.")
    name = graphene.String(required=True, max_length=100)
    fee_type = graphene.String(
        required=False, description="FFS or BUNDLE. Defaults to FFS."
    )
    currency = graphene.String(required=False, max_length=3)
    json_ext = ParsedJSONString(required=False)


class ActivateContractFeeScheduleInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="ContractFeeSchedule.uuid.")
    activate_contract = graphene.Boolean(
        required=False,
        description=(
            "Also activate the owning contract. Convenient because in "
            "FACILITY_PRICELIST mode a contract cannot be activated without an "
            "ACTIVE schedule."
        ),
    )


class ContractFeeItemInput(OpenIMISMutation.Input):
    schedule_id = graphene.String(required=True, description="ContractFeeSchedule.uuid.")
    id = graphene.String(required=False, description="ContractFeeItem.uuid.")
    service_id = graphene.String(
        required=False, description="medical.Service.id. Mutually exclusive with item."
    )
    item_id = graphene.String(
        required=False, description="medical.Item.uuid. Mutually exclusive with service."
    )
    amount = graphene.Decimal(max_digits=18, decimal_places=2, required=True)
    quantity = graphene.Int(required=False)
    overrule_max_amount = graphene.Decimal(
        max_digits=18, decimal_places=2, required=False
    )
    is_included = graphene.Boolean(required=False)


class ContractFeeBulkRowInput(InputObjectType):
    """One row of a bulk fee update.

    Identified by code rather than by id: the spreadsheet a payer hands over
    lists service and item codes, so that is what a row has to carry.
    """

    service_code = graphene.String(required=False, max_length=6)
    item_code = graphene.String(required=False, max_length=30)
    amount = graphene.Decimal(max_digits=18, decimal_places=2, required=True)
    quantity = graphene.Int(required=False)


class BulkUpdateFeesInput(OpenIMISMutation.Input):
    schedule_id = graphene.String(required=True, description="ContractFeeSchedule.uuid.")
    filename = graphene.String(required=False, max_length=255)
    rows = graphene.List(ContractFeeBulkRowInput, required=True)


class ResolveClaimScopeViolationInput(OpenIMISMutation.Input):
    id = graphene.String(required=True, description="ClaimScopeViolation.uuid.")
    resolution = graphene.String(required=True, max_length=255)

# ---------------------------------------------------------------------------
# Lookups
#
# Every mutation resolves its target through the model's manager rather than
# ``Model.objects.get(uuid=...)``, because HistoryModelManager annotates
# ``uuid=F("id")``; going around it would bypass the soft-delete filter and the
# audit-based caching.
# ---------------------------------------------------------------------------


def _fetch(model, uuid, label):
    obj = model.objects.filter(uuid=uuid).first()
    if obj is None:
        raise ValidationError(_("provider_contract.mutation.not_found") % {"label": label})
    return obj


def _contract(contract_id):
    return _fetch(ProviderContract, contract_id, _("contract"))


def _process(process_id):
    return _fetch(EmpanelmentProcess, process_id, _("empanelment process"))


def _schedule(schedule_id):
    return _fetch(ContractFeeSchedule, schedule_id, _("fee schedule"))


# ---------------------------------------------------------------------------
# Empanelment process
# ---------------------------------------------------------------------------


class CreateEmpanelmentProcessMutation(OpenIMISMutation):
    """Open an empanelment application for a provider."""

    _mutation_module = "provider_contract"
    _mutation_class = "CreateEmpanelmentProcessMutation"

    class Input(CreateEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_create_empanelment_process_perms",
        "provider_contract.mutation.failed_to_create_process",
        label=lambda d: d.get("reference_no", ""),
    )
    def async_mutate(cls, user, **data):
        service = EmpanelmentService(user)
        # The service wants the model, GraphQL only has the uuid.
        workflow = _fetch(
            EmpanelmentWorkflow, data.get("workflow_id"), _("empanelment workflow")
        )
        payload = dict(data, workflow=workflow)
        service.create(payload)
        return None


class UpdateEmpanelmentProcessMutation(OpenIMISMutation):
    """Edit a process that has not yet been decided."""

    _mutation_module = "provider_contract"
    _mutation_class = "UpdateEmpanelmentProcessMutation"

    class Input(UpdateEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_update_empanelment_process_perms",
        "provider_contract.mutation.failed_to_update_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        process = _process(data["id"])
        EmpanelmentService(user).update(process, data)
        return None


class SubmitEmpanelmentProcessMutation(OpenIMISMutation):
    """DRAFT -> SUBMITTED."""

    _mutation_module = "provider_contract"
    _mutation_class = "SubmitEmpanelmentProcessMutation"

    class Input(UpdateEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_submit_empanelment_process_perms",
        "provider_contract.mutation.failed_to_submit_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).submit(_process(data["id"]))
        return None


class AdvanceEmpanelmentProcessMutation(OpenIMISMutation):
    """Record the current stage's outcome and move to the next stage.

    Reaching the last stage leaves the process UNDER_REVIEW; it still has to be
    decided. That separation is deliberate and enforced in the service.
    """

    _mutation_module = "provider_contract"
    _mutation_class = "AdvanceEmpanelmentProcessMutation"

    class Input(AdvanceEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_advance_empanelment_process_perms",
        "provider_contract.mutation.failed_to_advance_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        outcome = data.get("outcome") or EmpanelmentStageTransition.Outcome.PASSED
        EmpanelmentService(user).advance(
            _process(data["id"]),
            outcome=outcome,
            comment=data.get("comment"),
        )
        return None


class DecideEmpanelmentProcessMutation(OpenIMISMutation):
    """Close the application with the committee's decision."""

    _mutation_module = "provider_contract"
    _mutation_class = "DecideEmpanelmentProcessMutation"

    class Input(DecideEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_decide_empanelment_process_perms",
        "provider_contract.mutation.failed_to_decide_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).decide(
            _process(data["id"]),
            approved=data["approved"],
            reason=data.get("reason"),
        )
        return None


class WithdrawEmpanelmentProcessMutation(OpenIMISMutation):
    """Withdraw an application the provider no longer wants to pursue."""

    _mutation_module = "provider_contract"
    _mutation_class = "WithdrawEmpanelmentProcessMutation"

    class Input(WithdrawEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_withdraw_empanelment_process_perms",
        "provider_contract.mutation.failed_to_withdraw_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).withdraw(
            _process(data["id"]), reason=data.get("reason")
        )
        return None


class DeleteEmpanelmentProcessMutation(OpenIMISMutation):
    """Soft-delete a process. Soft-deleted rows keep their place in the audit."""

    _mutation_module = "provider_contract"
    _mutation_class = "DeleteEmpanelmentProcessMutation"

    class Input(UpdateEmpanelmentProcessInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_delete_empanelment_process_perms",
        "provider_contract.mutation.failed_to_delete_process",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).delete(_process(data["id"]))
        return None


# ---------------------------------------------------------------------------
# Empanelment documents
# ---------------------------------------------------------------------------


class AddEmpanelmentDocumentMutation(OpenIMISMutation):
    """Attach an uploaded document to a process."""

    _mutation_module = "provider_contract"
    _mutation_class = "AddEmpanelmentDocumentMutation"

    class Input(AddEmpanelmentDocumentInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_add_empanelment_document_perms",
        "provider_contract.mutation.failed_to_add_document",
        label=lambda d: d.get("document_type_id", ""),
    )
    def async_mutate(cls, user, **data):
        document_type = _fetch(
            EmpanelmentDocumentType, data.get("document_type_id"), _("document type")
        )
        EmpanelmentService(user).add_document(
            _process(data["process_id"]), document_type, data
        )
        return None


class VerifyEmpanelmentDocumentMutation(OpenIMISMutation):
    """Accept an uploaded document."""

    _mutation_module = "provider_contract"
    _mutation_class = "VerifyEmpanelmentDocumentMutation"

    class Input(ReviewEmpanelmentDocumentInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_verify_empanelment_document_perms",
        "provider_contract.mutation.failed_to_verify_document",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).verify_document(
            _fetch(EmpanelmentProcessDocument, data["id"], _("document")),
            note=data.get("note"),
        )
        return None


class RejectEmpanelmentDocumentMutation(OpenIMISMutation):
    """Reject an uploaded document. A note is mandatory."""

    _mutation_module = "provider_contract"
    _mutation_class = "RejectEmpanelmentDocumentMutation"

    class Input(ReviewEmpanelmentDocumentInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_reject_empanelment_document_perms",
        "provider_contract.mutation.failed_to_reject_document",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        EmpanelmentService(user).reject_document(
            _fetch(EmpanelmentProcessDocument, data["id"], _("document")),
            note=data.get("note"),
        )
        return None


# ---------------------------------------------------------------------------
# Contract lifecycle
# ---------------------------------------------------------------------------


class CreateProviderContractMutation(OpenIMISMutation):
    """Draft a new contract. Starts in DRAFT; it prices nothing until active."""

    _mutation_module = "provider_contract"
    _mutation_class = "CreateProviderContractMutation"

    class Input(CreateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_create_provider_contract_perms",
        "provider_contract.mutation.failed_to_create_contract",
        label=lambda d: d.get("contract_number", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).create(data)
        return None


class UpdateProviderContractMutation(OpenIMISMutation):
    """Edit the mutable fields of a contract.

    Only a DRAFT contract is editable. Anything else needs amend(), which opens a
    new version rather than rewriting history.
    """

    _mutation_module = "provider_contract"
    _mutation_class = "UpdateProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_update_provider_contract_perms",
        "provider_contract.mutation.failed_to_update_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).update(_contract(data["id"]), data)
        return None


class SubmitProviderContractMutation(OpenIMISMutation):
    """DRAFT -> UNDER_NEGOTIATION."""

    _mutation_module = "provider_contract"
    _mutation_class = "SubmitProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_submit_provider_contract_perms",
        "provider_contract.mutation.failed_to_submit_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).submit(_contract(data["id"]))
        return None


class ApproveProviderContractMutation(OpenIMISMutation):
    """UNDER_NEGOTIATION -> SIGNED."""

    _mutation_module = "provider_contract"
    _mutation_class = "ApproveProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_approve_provider_contract_perms",
        "provider_contract.mutation.failed_to_approve_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).approve(
            _contract(data["id"]), date_signed=data.get("date_signed")
        )
        return None


class ActivateProviderContractMutation(OpenIMISMutation):
    """SIGNED -> ACTIVE. In FACILITY_PRICELIST mode an ACTIVE schedule is required."""

    _mutation_module = "provider_contract"
    _mutation_class = "ActivateProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_create_provider_contract_perms",
        "provider_contract.mutation.failed_to_activate_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).activate(_contract(data["id"]))
        return None


class SuspendProviderContractMutation(OpenIMISMutation):
    """Temporarily stop a contract pricing without terminating it."""

    _mutation_module = "provider_contract"
    _mutation_class = "SuspendProviderContractMutation"

    class Input(ContractReasonInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_update_provider_contract_perms",
        "provider_contract.mutation.failed_to_suspend_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).suspend(
            _contract(data["id"]), reason=data.get("reason")
        )
        return None


class ResumeProviderContractMutation(OpenIMISMutation):
    """SUSPENDED -> ACTIVE again."""

    _mutation_module = "provider_contract"
    _mutation_class = "ResumeProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_update_provider_contract_perms",
        "provider_contract.mutation.failed_to_resume_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).resume(_contract(data["id"]))
        return None


class AmendProviderContractMutation(OpenIMISMutation):
    """Open a new version of an agreed contract.

    Never edits in place: the old version is closed and a successor is created,
    so claims already priced against the old terms stay reproducible.
    """

    _mutation_module = "provider_contract"
    _mutation_class = "AmendProviderContractMutation"

    class Input(AmendProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_amend_provider_contract_perms",
        "provider_contract.mutation.failed_to_amend_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        changes = {
            key: value
            for key, value in data.items()
            if key != "id" and value is not None
        }
        ProviderContractService(user).amend(_contract(data["id"]), changes)
        return None


class RenewProviderContractMutation(OpenIMISMutation):
    """Renew into a new term. The old version is marked RENEWED, never deleted."""

    _mutation_module = "provider_contract"
    _mutation_class = "RenewProviderContractMutation"

    class Input(RenewProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_renew_provider_contract_perms",
        "provider_contract.mutation.failed_to_renew_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).renew(
            _contract(data["id"]),
            date_start=data.get("date_start"),
            date_end=data["date_end"],
            auto_renew=data.get("auto_renew"),
        )
        return None


class TerminateProviderContractMutation(OpenIMISMutation):
    """Close a contract early. A reason is mandatory."""

    _mutation_module = "provider_contract"
    _mutation_class = "TerminateProviderContractMutation"

    class Input(ContractReasonInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_terminate_provider_contract_perms",
        "provider_contract.mutation.failed_to_terminate_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).terminate(
            _contract(data["id"]), reason=data["reason"]
        )
        return None


class DeleteProviderContractMutation(OpenIMISMutation):
    """Soft-delete a contract that has never been agreed."""

    _mutation_module = "provider_contract"
    _mutation_class = "DeleteProviderContractMutation"

    class Input(UpdateProviderContractInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_delete_provider_contract_perms",
        "provider_contract.mutation.failed_to_delete_contract",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ProviderContractService(user).delete(_contract(data["id"]))
        return None


# ---------------------------------------------------------------------------
# Fee schedules
# ---------------------------------------------------------------------------


class CreateContractFeeScheduleMutation(OpenIMISMutation):
    """Add a DRAFT fee schedule to a contract."""

    _mutation_module = "provider_contract"
    _mutation_class = "CreateContractFeeScheduleMutation"

    class Input(CreateContractFeeScheduleInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_create_contract_fee_schedule_perms",
        "provider_contract.mutation.failed_to_create_fee_schedule",
        label=lambda d: d.get("name", ""),
    )
    def async_mutate(cls, user, **data):
        ContractFeeService(user).create_schedule(
            _contract(data["contract_id"]), data
        )
        return None


class ActivateContractFeeScheduleMutation(OpenIMISMutation):
    """Publish a fee schedule, materializing it into a pricelist where required."""

    _mutation_module = "provider_contract"
    _mutation_class = "ActivateContractFeeScheduleMutation"

    class Input(ActivateContractFeeScheduleInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_activate_contract_fee_schedule_perms",
        "provider_contract.mutation.failed_to_activate_fee_schedule",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ContractFeeService(user).activate_schedule(
            _schedule(data["id"]),
            activate_contract=data.get("activate_contract", False),
        )
        return None


class SaveContractFeeItemMutation(OpenIMISMutation):
    """Create or update one fee row.

    Named ``save`` rather than ``create``/``update`` because it is a genuine
    upsert keyed on (schedule, service|item): re-posting the same row amends the
    price instead of duplicating it.
    """

    _mutation_module = "provider_contract"
    _mutation_class = "SaveContractFeeItemMutation"

    class Input(ContractFeeItemInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_update_contract_fee_item_perms",
        "provider_contract.mutation.failed_to_save_fee_item",
        label=lambda d: d.get("id") or d.get("service_id") or "",
    )
    def async_mutate(cls, user, **data):
        from medical.models import Item, Service

        schedule = _schedule(data["schedule_id"])
        service = (
            Service.objects.filter(id=data["service_id"]).first()
            if data.get("service_id")
            else None
        )
        item = (
            Item.objects.filter(uuid=data["item_id"]).first()
            if data.get("item_id")
            else None
        )
        ContractFeeService(user).upsert_fee_item(
            schedule,
            service=service,
            item=item,
            amount=data["amount"],
            quantity=data.get("quantity", 1),
            overrule_max_amount=data.get("overrule_max_amount"),
        )
        return None


class DeleteContractFeeItemMutation(OpenIMISMutation):
    """Soft-delete a fee row. Refused once its schedule is ACTIVE."""

    _mutation_module = "provider_contract"
    _mutation_class = "DeleteContractFeeItemMutation"

    class Input(ContractFeeItemInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_delete_contract_fee_item_perms",
        "provider_contract.mutation.failed_to_delete_fee_item",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ContractFeeService(user).delete_fee_item(
            _fetch(ContractFeeItem, data["id"], _("fee item"))
        )
        return None


class BulkUpdateContractFeesMutation(OpenIMISMutation):
    """Apply many fee rows at once, reporting per-row outcomes.

    Import-style on purpose: one bad row in a 400-row spreadsheet must not
    discard the other 399, because the operator then fixes and re-uploads only
    what failed. Returns a per-row error list rather than failing the call.
    """

    _mutation_module = "provider_contract"
    _mutation_class = "BulkUpdateContractFeesMutation"

    class Input(BulkUpdateFeesInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_bulk_update_fees_perms",
        "provider_contract.mutation.failed_to_bulk_update_fees",
        label=lambda d: d.get("filename", ""),
    )
    def async_mutate(cls, user, **data):
        rows = [dict(row) for row in data.get("rows") or []]
        ContractFeeService(user).bulk_update_fees(
            _schedule(data["schedule_id"]), rows
        )
        return None


# ---------------------------------------------------------------------------
# Gate queue triage
# ---------------------------------------------------------------------------


class ResolveClaimScopeViolationMutation(OpenIMISMutation):
    """Mark a gate finding as reviewed and genuinely fixed."""

    _mutation_module = "provider_contract"
    _mutation_class = "ResolveClaimScopeViolationMutation"

    class Input(ResolveClaimScopeViolationInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_resolve_claim_scope_violation_perms",
        "provider_contract.mutation.failed_to_resolve_violation",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ClaimScopeViolationService(user).resolve(
            _fetch(ClaimScopeViolation, data["id"], _("violation")),
            resolution=data["resolution"],
        )
        return None


class WaiveClaimScopeViolationMutation(OpenIMISMutation):
    """Accept a gate finding without fixing it. A reason is mandatory.

    Distinct from resolve on purpose: a waived finding stays on the record as an
    accepted deviation, which is a different fact from "this was fixed".
    """

    _mutation_module = "provider_contract"
    _mutation_class = "WaiveClaimScopeViolationMutation"

    class Input(ResolveClaimScopeViolationInput):
        pass

    @classmethod
    @pce_mutation(
        "gql_mutation_waive_claim_scope_violation_perms",
        "provider_contract.mutation.failed_to_waive_violation",
        label=lambda d: d.get("id", ""),
    )
    def async_mutate(cls, user, **data):
        ClaimScopeViolationService(user).waive(
            _fetch(ClaimScopeViolation, data["id"], _("violation")),
            reason=data["resolution"],
        )
        return None


class Mutation(graphene.ObjectType):
    create_empanelment_process = CreateEmpanelmentProcessMutation.Field()
    update_empanelment_process = UpdateEmpanelmentProcessMutation.Field()
    submit_empanelment_process = SubmitEmpanelmentProcessMutation.Field()
    advance_empanelment_process = AdvanceEmpanelmentProcessMutation.Field()
    decide_empanelment_process = DecideEmpanelmentProcessMutation.Field()
    withdraw_empanelment_process = WithdrawEmpanelmentProcessMutation.Field()
    delete_empanelment_process = DeleteEmpanelmentProcessMutation.Field()

    add_empanelment_document = AddEmpanelmentDocumentMutation.Field()
    verify_empanelment_document = VerifyEmpanelmentDocumentMutation.Field()
    reject_empanelment_document = RejectEmpanelmentDocumentMutation.Field()

    create_provider_contract = CreateProviderContractMutation.Field()
    update_provider_contract = UpdateProviderContractMutation.Field()
    submit_provider_contract = SubmitProviderContractMutation.Field()
    approve_provider_contract = ApproveProviderContractMutation.Field()
    activate_provider_contract = ActivateProviderContractMutation.Field()
    suspend_provider_contract = SuspendProviderContractMutation.Field()
    resume_provider_contract = ResumeProviderContractMutation.Field()
    amend_provider_contract = AmendProviderContractMutation.Field()
    renew_provider_contract = RenewProviderContractMutation.Field()
    terminate_provider_contract = TerminateProviderContractMutation.Field()
    delete_provider_contract = DeleteProviderContractMutation.Field()

    create_contract_fee_schedule = CreateContractFeeScheduleMutation.Field()
    activate_contract_fee_schedule = ActivateContractFeeScheduleMutation.Field()
    save_contract_fee_item = SaveContractFeeItemMutation.Field()
    delete_contract_fee_item = DeleteContractFeeItemMutation.Field()
    bulk_update_contract_fees = BulkUpdateContractFeesMutation.Field()

    resolve_claim_scope_violation = ResolveClaimScopeViolationMutation.Field()
    waive_claim_scope_violation = WaiveClaimScopeViolationMutation.Field()
