"""GraphQL read layer for provider empanelment and contracts.

Conventions followed here (see AGENTS section 5):

* collection queries are plural ``snake_case`` (``provider_contracts``), the
  single-object fetch is singular (``provider_contract``), and the
  full-text-search helper is suffixed ``_str``;
* row security is delegated to ``<Model>.get_queryset(queryset, info)``, which
  is the mixin in :mod:`provider_contract.models` -- never reimplemented here,
  or the district filter would be silently dropped;
* permissions are checked in ``get_queryset`` rather than in a resolver,
  because a connection field has no resolver to hang them on. Raising
  ``PermissionDenied`` there means an unauthorised caller cannot page through
  the collection a row at a time either.

The two gate queries (``check_provider_empanelment`` and ``applicable_fee``)
are read paths. They never mutate, never raise on a negative result, and always
return a verdict object with a machine-readable ``reasonCode`` (ADR-006).
"""

import graphene
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils.translation import gettext as _
from graphene_django import DjangoObjectType

from core import ExtendedConnection, prefix_filterset
from core.schema import OrderedDjangoFilterConnectionField
from location.models import HealthFacility
from location.schema import HealthFacilityGQLType
from medical.models import Item, Service
from medical.schema import ItemGQLType, ServiceGQLType
from product.schema import ProductGQLType

from ..apps import ProviderContractConfig
from ..models import (
    ClaimScopeViolation,
    ContractBenefitPackage,
    ContractFeeBulkOperation,
    ContractFeeItem,
    ContractFeeSchedule,
    ContractServiceCategory,
    EmpanelmentDocumentType,
    EmpanelmentProcess,
    EmpanelmentProcessDocument,
    EmpanelmentStage,
    EmpanelmentStageTransition,
    EmpanelmentWorkflow,
    ProviderContract,
)

DATE_FILTERS = ["exact", "lt", "lte", "gt", "gte"]
TEXT_FILTERS = ["exact", "istartswith", "icontains", "iexact"]


def _require(user, perms):
    """Raise unless ``user`` holds ``perms``."""
    if not user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))


def _pending_mutation_id(instance, perms, info):
    """``client_mutation_id`` of the newest still-pending mutation, if any.

    openIMIS reports every mutation through ``MutationLog`` so a client can
    correlate a request with its result; the convention is that each exposed
    entity's GQLType exposes the pending id.
    """
    if not info.context.user.has_perms(perms):
        raise PermissionDenied(_("unauthorized"))
    mutation = (
        instance.mutations.select_related("mutation")
        .filter(mutation__status=0)
        .first()
    )
    return mutation.mutation.client_mutation_id if mutation else None


class PCEObjectType(DjangoObjectType):
    """Shared plumbing: the pending-mutation id every entity exposes."""

    class Meta:
        abstract = True

    client_mutation_id = graphene.String()


# ---------------------------------------------------------------------------
# Catalogue entities. These are global reference data with no district scope,
# so they deliberately have no location_prefix and no row-security filter.
# ---------------------------------------------------------------------------


class EmpanelmentWorkflowGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentWorkflow
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "code": TEXT_FILTERS,
            "name": TEXT_FILTERS,
            "status": ["exact"],
            "is_default": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return queryset.filter(*EmpanelmentWorkflow.filter_validity())

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_empanelment_processes_perms, info
        )


class EmpanelmentStageGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentStage
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "code": TEXT_FILTERS,
            "name": TEXT_FILTERS,
            "sequence": ["exact", "lt", "gt"],
            "outcome": ["exact"],
            "is_decision_stage": ["exact"],
            "requires_all_documents": ["exact"],
            **prefix_filterset(
                "workflow__", EmpanelmentWorkflowGQLType._meta.filter_fields
            ),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return EmpanelmentStage.filter_validity(queryset)

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_empanelment_processes_perms, info
        )


class EmpanelmentDocumentTypeGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentDocumentType
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "code": TEXT_FILTERS,
            "name": TEXT_FILTERS,
            "is_mandatory": ["exact"],
            "requires_expiry": ["exact"],
            "sort_order": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return queryset.filter(*EmpanelmentDocumentType.filter_validity())

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_empanelment_processes_perms, info
        )


# ---------------------------------------------------------------------------
# Empanelment workflow execution. Row security comes from the models.
# ---------------------------------------------------------------------------


class EmpanelmentStageTransitionGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentStageTransition
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "outcome": ["exact"],
            "transition_date": DATE_FILTERS,
            "comment": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return EmpanelmentStageTransition.get_queryset(queryset, info)


class EmpanelmentProcessDocumentGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentProcessDocument
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "status": ["exact"],
            "file_reference": TEXT_FILTERS,
            "date_uploaded": DATE_FILTERS,
            "date_expires": DATE_FILTERS,
            "date_reviewed": DATE_FILTERS,
            "review_note": ["exact"],
            **prefix_filterset(
                "process__", {"reference_no": TEXT_FILTERS, "status": ["exact"]}
            ),
            **prefix_filterset(
                "document_type__",
                {"code": TEXT_FILTERS, "name": TEXT_FILTERS, "is_mandatory": ["exact"]},
            ),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return EmpanelmentProcessDocument.get_queryset(queryset, info)

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_empanelment_processes_perms, info
        )


class EmpanelmentProcessGQLType(PCEObjectType):
    class Meta:
        model = EmpanelmentProcess
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "reference_no": TEXT_FILTERS,
            "status": ["exact"],
            "date_submitted": DATE_FILTERS,
            "date_decided": DATE_FILTERS,
            "valid_until": DATE_FILTERS,
            "decision_reason": ["exact"],
            **prefix_filterset(
                "provider__", HealthFacilityGQLType._meta.filter_fields
            ),
            **prefix_filterset(
                "workflow__", EmpanelmentWorkflowGQLType._meta.filter_fields
            ),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return EmpanelmentProcess.get_queryset(queryset, info)

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_empanelment_processes_perms, info
        )

    def resolve_transitions(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_empanelment_processes_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.transitions.all()

    def resolve_documents(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_empanelment_processes_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.documents.all()


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class ContractBenefitPackageGQLType(DjangoObjectType):
    class Meta:
        model = ContractBenefitPackage
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "is_included": ["exact"],
            **prefix_filterset("product__", ProductGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        return ContractBenefitPackage.get_queryset(queryset, info)


class ContractServiceCategoryGQLType(DjangoObjectType):
    class Meta:
        model = ContractServiceCategory
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "category_code": TEXT_FILTERS,
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        return ContractServiceCategory.get_queryset(queryset, info)


class ContractFeeItemGQLType(DjangoObjectType):
    class Meta:
        model = ContractFeeItem
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "amount": ["exact"],
            "quantity": ["exact"],
            "is_included": ["exact"],
            "overrule_max_amount": ["exact"],
            **prefix_filterset("service__", ServiceGQLType._meta.filter_fields),
            **prefix_filterset("item__", ItemGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_contract_fee_schedules_perms)
        return ContractFeeItem.get_queryset(queryset, info)


class ContractFeeBulkOperationGQLType(DjangoObjectType):
    class Meta:
        model = ContractFeeBulkOperation
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "operation_type": ["exact"],
            "status": ["exact"],
            "filename": TEXT_FILTERS,
            "total_rows": ["exact"],
            "succeeded_rows": ["exact"],
            "failed_rows": ["exact"],
            "date_started": DATE_FILTERS,
            "date_finished": DATE_FILTERS,
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_contract_fee_schedules_perms)
        return ContractFeeBulkOperation.get_queryset(queryset, info)


class ContractFeeScheduleGQLType(PCEObjectType):
    class Meta:
        model = ContractFeeSchedule
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "name": TEXT_FILTERS,
            "fee_type": ["exact"],
            "status": ["exact"],
            "currency": ["exact"],
            "resolution_mode": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_contract_fee_schedules_perms)
        return ContractFeeSchedule.get_queryset(queryset, info)

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_contract_fee_schedules_perms, info
        )

    def resolve_fee_items(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_contract_fee_schedules_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.fee_items.all()


class ProviderContractGQLType(PCEObjectType):
    class Meta:
        model = ProviderContract
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "contract_number": TEXT_FILTERS,
            "status": ["exact"],
            "version_no": ["exact"],
            "currency": ["exact"],
            "date_signed": DATE_FILTERS,
            "date_start": DATE_FILTERS,
            "date_end": DATE_FILTERS,
            "date_renewal": DATE_FILTERS,
            "date_terminated": DATE_FILTERS,
            "auto_renew": ["exact"],
            "notice_period_days": ["exact"],
            "notes": ["exact"],
            **prefix_filterset("provider__", HealthFacilityGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        return ProviderContract.get_queryset(queryset, info)

    def resolve_client_mutation_id(self, info):
        return _pending_mutation_id(
            self, ProviderContractConfig.gql_query_provider_contracts_perms, info
        )

    def resolve_fee_schedules(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_contract_fee_schedules_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.fee_schedules.all()

    def resolve_benefit_packages(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_provider_contracts_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.benefit_packages.all()

    def resolve_service_categories(self, info):
        if not info.context.user.has_perms(
            ProviderContractConfig.gql_query_provider_contracts_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        return self.service_categories.all()


# ---------------------------------------------------------------------------
# The gate queue
# ---------------------------------------------------------------------------


class ClaimScopeViolationGQLType(DjangoObjectType):
    class Meta:
        model = ClaimScopeViolation
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],  # uuid is a property, not a column -- see AGENTS 4.6
            "service_code": TEXT_FILTERS,
            "date_of_service": DATE_FILTERS,
            "reason_code": ["exact"],
            "severity": ["exact"],
            "is_resolved": ["exact"],
            "date_resolved": DATE_FILTERS,
            "resolution": ["exact"],
            **prefix_filterset("provider__", HealthFacilityGQLType._meta.filter_fields),
            **prefix_filterset("claim__", {"code": TEXT_FILTERS, "status": ["exact"]}),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        _require(info.context.user, ProviderContractConfig.gql_query_claim_scope_violations_perms)
        return ClaimScopeViolation.get_queryset(queryset, info)


# ---------------------------------------------------------------------------
# Gate verdicts. Plain ObjectTypes: a verdict is computed, never stored, so it
# has no model and is never filtered.
# ---------------------------------------------------------------------------


def _resolve_provider(provider_id):
    """HealthFacility.uuid is a CharField(36), so the argument is a String."""
    return HealthFacility.objects.filter(uuid=provider_id).first()


def _resolve_service(service_id):
    if not service_id:
        return None
    return Service.objects.filter(id=service_id).first()


def _resolve_item(item_id):
    if not item_id:
        return None
    return Item.objects.filter(uuid=item_id).first()


def _resolve_str(model, info, kwargs):
    """Full-text search helper shared by the ``*_str`` queries."""
    _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
    term = (kwargs.get("str") or "").strip()
    queryset = model.get_queryset(model.objects.all(), info)
    if not term:
        return queryset.none()
    # Portable across PostgreSQL and MSSQL: no full-text extension and no
    # case-folding helper, so OR the searchable text columns.
    lookups = Q()
    searchable = {"contract_number", "reference_no", "notes", "status"}
    for field in searchable & {f.name for f in model._meta.fields}:
        lookups |= Q(**{f"{field}__icontains": term})
    return queryset.filter(lookups)


class EmpanelmentVerdictGQLType(graphene.ObjectType):
    """Outcome of one gate evaluation.

    Deliberately flat and always fully populated. A caller has to be able to
    branch on ``reasonCode`` without inspecting prose, and ``empanelled`` is
    redundant with it on purpose so that a verdict survives being logged and
    re-read by a human.
    """

    empanelled = graphene.Boolean(
        required=True,
        description="True when a contract covering the provider was in force.",
    )
    contract = graphene.String(
        description="contract_number of the contract that covers the provider, if any."
    )
    contract_uuid = graphene.String()
    reason_code = graphene.String(
        description=(
            "Machine-readable reason. One of the ClaimScopeViolation.ReasonCode "
            "values, or null when the provider is empanelled."
        )
    )
    reason = graphene.String(description="Human-readable explanation of reason_code.")
    severity = graphene.String(
        description="ClaimScopeViolation.Severity: BLOCKING or WARNING."
    )
    date_of_service = graphene.Date()
    resolution_mode = graphene.String(
        description="Which mechanism priced the claim: FACILITY_PRICELIST or CONTRACT_CALCULE."
    )


class ApplicableFeeGQLType(graphene.ObjectType):
    """The contract fee for a service, and how it was reached."""

    fee = graphene.Decimal()
    amount = graphene.Decimal()
    currency = graphene.String()
    quantity = graphene.Int()
    contract = graphene.String()
    contract_uuid = graphene.String()
    fee_schedule = graphene.String()
    reason_code = graphene.String()
    reason = graphene.String()
    severity = graphene.String()
    date_of_service = graphene.Date()
    resolution_mode = graphene.String(
        description=(
            "Which mechanism resolved the fee. Reported so an adjudicator can "
            "tell a materialized pricelist price from a calcrule price."
        )
    )


class Query(graphene.ObjectType):
    provider_contracts = OrderedDjangoFilterConnectionField(
        ProviderContractGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    provider_contracts_str = OrderedDjangoFilterConnectionField(
        ProviderContractGQLType,
        str=graphene.String(),
    )
    provider_contract = graphene.Field(ProviderContractGQLType, id=graphene.String(required=True))

    contract_fee_schedules = OrderedDjangoFilterConnectionField(
        ContractFeeScheduleGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    contract_fee_items = OrderedDjangoFilterConnectionField(
        ContractFeeItemGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    contract_fee_bulk_operations = OrderedDjangoFilterConnectionField(
        ContractFeeBulkOperationGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    contract_benefit_packages = OrderedDjangoFilterConnectionField(
        ContractBenefitPackageGQLType
    )
    contract_service_categories = OrderedDjangoFilterConnectionField(
        ContractServiceCategoryGQLType
    )

    empanelment_processes = OrderedDjangoFilterConnectionField(
        EmpanelmentProcessGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    empanelment_processes_str = OrderedDjangoFilterConnectionField(
        EmpanelmentProcessGQLType,
        str=graphene.String(),
    )
    empanelment_process = graphene.Field(
        EmpanelmentProcessGQLType, id=graphene.String(required=True)
    )
    empanelment_workflows = OrderedDjangoFilterConnectionField(EmpanelmentWorkflowGQLType)
    empanelment_stages = OrderedDjangoFilterConnectionField(EmpanelmentStageGQLType)
    empanelment_document_types = OrderedDjangoFilterConnectionField(
        EmpanelmentDocumentTypeGQLType
    )
    empanelment_stage_transitions = OrderedDjangoFilterConnectionField(
        EmpanelmentStageTransitionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )
    empanelment_process_documents = OrderedDjangoFilterConnectionField(
        EmpanelmentProcessDocumentGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    claim_scope_violations = OrderedDjangoFilterConnectionField(
        ClaimScopeViolationGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    # --- read-only gate queries -----------------------------------------
    check_provider_empanelment = graphene.Field(
        EmpanelmentVerdictGQLType,
        provider_id=graphene.String(
            required=True,
            description="HealthFacility.uuid. A CharField(36), so String.",
        ),
        service_id=graphene.String(
            required=False, description="medical.Service.id for a per-service check."
        ),
        on_date=graphene.Date(
            required=False,
            description="Date of service. Defaults to today.",
        ),
        description="Read-only. Whether a provider was empanelled on a date.",
    )

    applicable_fee = graphene.Field(
        ApplicableFeeGQLType,
        provider_id=graphene.String(required=True, description="HealthFacility.uuid."),
        service_id=graphene.String(required=False, description="medical.Service.id."),
        item_id=graphene.String(required=False, description="medical.Item.uuid."),
        on_date=graphene.Date(required=False, description="Date of service."),
        description="Read-only. The contract fee for a service, and why.",
    )

    def resolve_provider_contract(self, info, id):
        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        return ProviderContract.get_queryset(
            ProviderContract.objects.all(), info
        ).filter(uuid=id).first()

    def resolve_empanelment_process(self, info, id):
        _require(info.context.user, ProviderContractConfig.gql_query_empanelment_processes_perms)
        return EmpanelmentProcess.get_queryset(
            EmpanelmentProcess.objects.all(), info
        ).filter(uuid=id).first()

    def resolve_check_provider_empanelment(
        self, info, provider_id, service_id=None, on_date=None
    ):
        from ..services import check_provider_empanelment

        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        provider = _resolve_provider(provider_id)
        service = _resolve_service(service_id)
        return check_provider_empanelment(provider, service=service, on_date=on_date)

    def resolve_applicable_fee(
        self, info, provider_id, service_id=None, item_id=None, on_date=None
    ):
        from ..services import applicable_fee

        _require(info.context.user, ProviderContractConfig.gql_query_provider_contracts_perms)
        provider = _resolve_provider(provider_id)
        service = _resolve_service(service_id)
        item = _resolve_item(item_id)
        return applicable_fee(provider, service=service, item=item, on_date=on_date)

    def resolve_provider_contracts_str(self, info, **kwargs):
        return _resolve_str(ProviderContract, info, kwargs)

    def resolve_empanelment_processes_str(self, info, **kwargs):
        return _resolve_str(EmpanelmentProcess, info, kwargs)


__all__ = [
    "ApplicableFeeGQLType",
    "ClaimScopeViolationGQLType",
    "ContractBenefitPackageGQLType",
    "ContractFeeBulkOperationGQLType",
    "ContractFeeItemGQLType",
    "ContractFeeScheduleGQLType",
    "ContractServiceCategoryGQLType",
    "EmpanelmentDocumentTypeGQLType",
    "EmpanelmentProcessDocumentGQLType",
    "EmpanelmentProcessGQLType",
    "EmpanelmentStageGQLType",
    "EmpanelmentStageTransitionGQLType",
    "EmpanelmentVerdictGQLType",
    "EmpanelmentWorkflowGQLType",
    "ProviderContractGQLType",
    "Query",
]
