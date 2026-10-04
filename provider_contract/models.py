"""Provider empanelment and contract lifecycle entities for openIMIS.

Design notes that are easy to get wrong and therefore worth stating here:

* Every model below owns a UUID primary key via ``core.models.HistoryModel``.
  ``uuid`` is a *property* aliasing ``id`` on that base class, so no model here
  declares a ``uuid`` field. Declaring one would shadow the property and break
  ``HistoryModelManager``, which annotates ``uuid=F("id")``.
* Cross-module references use string paths so Django resolves them lazily. The
  referenced legacy models (``location.HealthFacility``, ``medical.Service``,
  ``medical.Item``, ``product.Product``, ``medical_pricelist.*``,
  ``claim.Claim``) all use ``AutoField`` primary keys and ``validity_from`` /
  ``validity_to`` columns. Those are a different vocabulary from the
  ``date_valid_from`` / ``date_valid_to`` columns owned here. Never filter one
  with the other's column names.
* ``openIMIS`` supports PostgreSQL and MSSQL, so uniqueness that is conditional
  on a status ("one ACTIVE contract per facility") is enforced in services
  inside a transaction, not with a partial unique index (ADR-002). Plain
  ``unique=True`` and ``CheckConstraint`` are used, which both engines support.
* Row-level security is applied through ``location_prefix`` on
  ``ProviderContractQueryMixin``. The prefix is the FK path all the way to
  ``location.Location``.
"""

from datetime import datetime as py_datetime
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from graphql import ResolveInfo
from location.models import LocationManager

from core import models as core_models

# Sentinel used to return an empty queryset for anonymous callers under row
# security. The legacy openIMIS models use `id=-1`, but that is invalid on a
# UUID primary key and raises at query-evaluation time, so a nil UUID is used.
EMPTY_UUID = "00000000-0000-0000-0000-000000000000"


class ProviderContractQueryMixin:
    """District-level row security, applied to every owned model.

    ``build_user_location_filter_query`` compares ``prefix`` against allowed
    location ids, so ``location_prefix`` must name the path to
    ``location.Location`` (for example ``provider__location``), not the path to
    the provider itself.

    A note on the actor that gets handed to the location manager.
    ``LocationManager.build_user_location_filter_query`` applies a filter only
    when it is given an ``InteractiveUser``; for anything else it logs
    ``Access without filter`` and returns the queryset **unfiltered**.
    ``claim.Claim.get_queryset`` passes ``user._u``, but ``User._u`` resolves to
    ``self.i_user or self.officer or self.claim_admin or self.t_user``, none of
    which is guaranteed to be the ``InteractiveUser`` the manager checks for --
    so that call does not reliably satisfy the ``isinstance`` test. Rather than
    inherit that, ``_interactive_user_for`` resolves the actor explicitly and
    the queryset is emptied when it cannot, so an unrecognised actor sees
    nothing instead of everything.
    """

    location_prefix = None

    @classmethod
    def _interactive_user_for(cls, user):
        """Coerce the actor to the InteractiveUser the location manager wants.

        ``LocationManager.build_user_location_filter_query`` applies a filter
        only for an ``InteractiveUser``. For anything else it logs
        ``Access without filter`` and returns the queryset **unfiltered**, so
        this must never hand it a type it will silently ignore.

        ``core.User`` is resolved through its ``i_user`` relation, which is the
        only one of ``i_user``/``officer``/``claim_admin``/``t_user`` that
        points at an ``InteractiveUser``; the other three are sibling legacy
        models and carry no district scoping of their own. ``None`` is returned
        when no InteractiveUser can be found so the caller can fail closed.
        """
        if isinstance(user, core_models.InteractiveUser):
            return user
        i_user = getattr(user, "i_user", None)
        if isinstance(i_user, core_models.InteractiveUser):
            return i_user
        return None

    @classmethod
    def get_queryset(cls, queryset, user):
        queryset = cls.filter_queryset(queryset)
        # GraphQL calls with an info object while Rest calls with the user itself
        if isinstance(user, ResolveInfo):
            user = user.context.user
        # InteractiveUser is not a Django auth user and has no is_anonymous,
        # so this is only meaningful for a real auth principal.
        is_anonymous = not isinstance(
            user, core_models.InteractiveUser
        ) and getattr(user, "is_anonymous", False)
        if settings.ROW_SECURITY and is_anonymous:
            return queryset.filter(id=EMPTY_UUID)
        if settings.ROW_SECURITY:
            filter_user = cls._interactive_user_for(user)
            if filter_user is None:
                # Fail closed. Handing the manager an actor it does not
                # recognise would return every row in every district.
                return queryset.filter(id=EMPTY_UUID)
            queryset = LocationManager().build_user_location_filter_query(
                filter_user,
                prefix=cls.location_prefix,
                queryset=queryset,
                loc_types=["D"],
            )
        return queryset


class ProviderContractModel(ProviderContractQueryMixin, core_models.HistoryModel):
    """Base for owned, non-effective-dated records."""

    class Meta:
        abstract = True


class ProviderContractBusinessModel(
    ProviderContractQueryMixin, core_models.HistoryBusinessModel
):
    """Base for owned, effective-dated records.

    ``ValidityMixin`` inherits a ``filter_validity`` implementation from
    ``OpenIMISHistoryMixin`` that filters on ``active`` and
    ``date_deactivated``. Those columns belong to ``OpenIMISModel``;
    ``HistoryModel`` uses ``is_deleted``. The inherited helper therefore raises
    ``FieldError`` here and is replaced with one that matches the vocabulary
    actually used by this base class.
    """

    @classmethod
    def filter_validity(cls, queryset=None, date=None, **kwargs):
        date = date or py_datetime.now()
        if queryset is None:
            queryset = cls.objects.all()
        return queryset.filter(
            Q(date_valid_from__lte=date)
            & (Q(date_valid_to__isnull=True) | Q(date_valid_to__gt=date))
        )

    class Meta:
        abstract = True


class EmpanelmentWorkflow(ProviderContractModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        RETIRED = "RETIRED", "Retired"

    code = models.CharField(db_column="Code", max_length=20, unique=True)
    name = models.CharField(db_column="Name", max_length=100)
    description = models.CharField(
        db_column="Description", max_length=255, blank=True, null=True
    )
    status = models.CharField(
        db_column="Status", max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    is_default = models.BooleanField(db_column="IsDefault", default=False)
    sort_order = models.IntegerField(db_column="SortOrder", default=1)

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentWorkflow"
        ordering = ["sort_order", "code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class EmpanelmentStage(ProviderContractBusinessModel):
    class Outcome(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    workflow = models.ForeignKey(
        "provider_contract.EmpanelmentWorkflow",
        models.DO_NOTHING,
        related_name="stages",
        db_column="WorkflowUUID",
    )
    code = models.CharField(db_column="Code", max_length=20)
    name = models.CharField(db_column="Name", max_length=100)
    description = models.CharField(
        db_column="Description", max_length=255, blank=True, null=True
    )
    sequence = models.IntegerField(db_column="Sequence", default=1)
    outcome = models.CharField(
        db_column="Outcome", max_length=10, choices=Outcome.choices, default=Outcome.PENDING
    )
    sla_days = models.IntegerField(db_column="SlaDays", blank=True, null=True)
    is_decision_stage = models.BooleanField(db_column="IsDecisionStage", default=False)
    requires_all_documents = models.BooleanField(
        db_column="RequiresAllDocuments", default=True
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentStage"
        ordering = ["workflow_id", "sequence", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "code"], name="pce_stage_unique_per_workflow"
            )
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class EmpanelmentProcess(ProviderContractBusinessModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"
        EXPIRED = "EXPIRED", "Expired"

    location_prefix = "provider__location"

    provider = models.ForeignKey(
        "location.HealthFacility",
        models.DO_NOTHING,
        related_name="+",
        db_column="HfID",
    )
    workflow = models.ForeignKey(
        "provider_contract.EmpanelmentWorkflow",
        models.DO_NOTHING,
        related_name="processes",
        db_column="WorkflowUUID",
    )
    reference_no = models.CharField(
        db_column="ReferenceNo", max_length=30, unique=True
    )
    status = models.CharField(
        db_column="Status", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    current_stage = models.ForeignKey(
        "provider_contract.EmpanelmentStage",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="CurrentStageUUID",
    )
    date_submitted = models.DateTimeField(
        db_column="DateSubmitted", blank=True, null=True
    )
    date_decided = models.DateTimeField(
        db_column="DateDecided", blank=True, null=True
    )
    decided_by = models.ForeignKey(
        "core.User",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="DecidedByUUID",
    )
    decision_reason = models.CharField(
        db_column="DecisionReason", max_length=255, blank=True, null=True
    )
    valid_until = models.DateField(db_column="ValidUntil", blank=True, null=True)

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentProcess"
        ordering = ["-date_created"]
        indexes = [
            models.Index(fields=["provider", "status"], name="pce_process_provider_status"),
        ]

    def __str__(self):
        return f"{self.reference_no} {self.get_status_display()}"


class EmpanelmentStageTransition(ProviderContractModel):
    class Outcome(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    location_prefix = "process__provider__location"

    process = models.ForeignKey(
        "provider_contract.EmpanelmentProcess",
        models.DO_NOTHING,
        related_name="transitions",
        db_column="ProcessUUID",
    )
    from_stage = models.ForeignKey(
        "provider_contract.EmpanelmentStage",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="FromStageUUID",
    )
    to_stage = models.ForeignKey(
        "provider_contract.EmpanelmentStage",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ToStageUUID",
    )
    outcome = models.CharField(
        db_column="Outcome", max_length=10, choices=Outcome.choices, default=Outcome.PENDING
    )
    transition_date = models.DateTimeField(
        db_column="TransitionDate", default=py_datetime.now
    )
    comment = models.CharField(
        db_column="Comment", max_length=255, blank=True, null=True
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentStageTransition"
        ordering = ["transition_date"]
        indexes = [
            models.Index(fields=["process", "transition_date"], name="pce_transition_process_date"),
        ]

    def __str__(self):
        return f"{self.process.reference_no} {self.get_outcome_display()}"


class EmpanelmentDocumentType(ProviderContractModel):
    code = models.CharField(db_column="Code", max_length=30, unique=True)
    name = models.CharField(db_column="Name", max_length=100)
    description = models.CharField(
        db_column="Description", max_length=255, blank=True, null=True
    )
    is_mandatory = models.BooleanField(db_column="IsMandatory", default=False)
    requires_expiry = models.BooleanField(db_column="RequiresExpiry", default=False)
    validity_months = models.IntegerField(
        db_column="ValidityMonths", blank=True, null=True
    )
    sort_order = models.IntegerField(db_column="SortOrder", default=1)

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentDocumentType"
        ordering = ["sort_order", "code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class EmpanelmentProcessDocument(ProviderContractBusinessModel):
    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"
        EXPIRED = "EXPIRED", "Expired"
        REPLACED = "REPLACED", "Replaced"

    location_prefix = "process__provider__location"

    process = models.ForeignKey(
        "provider_contract.EmpanelmentProcess",
        models.DO_NOTHING,
        related_name="documents",
        db_column="ProcessUUID",
    )
    document_type = models.ForeignKey(
        "provider_contract.EmpanelmentDocumentType",
        models.DO_NOTHING,
        related_name="documents",
        db_column="DocumentTypeUUID",
    )
    status = models.CharField(
        db_column="Status", max_length=10, choices=Status.choices, default=Status.UPLOADED
    )
    file_reference = models.CharField(
        db_column="FileReference", max_length=255, blank=True, null=True
    )
    date_uploaded = models.DateTimeField(
        db_column="DateUploaded", default=py_datetime.now
    )
    date_expires = models.DateField(db_column="DateExpires", blank=True, null=True)
    reviewed_by = models.ForeignKey(
        "core.User",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ReviewedByUUID",
    )
    date_reviewed = models.DateTimeField(
        db_column="DateReviewed", blank=True, null=True
    )
    review_note = models.CharField(
        db_column="ReviewNote", max_length=255, blank=True, null=True
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentProcessDocument"
        ordering = ["-date_uploaded"]
        indexes = [
            models.Index(fields=["process", "status"], name="pce_document_process_status"),
        ]

    def __str__(self):
        return f"{self.document_type.code} {self.get_status_display()}"


class ProviderContract(ProviderContractBusinessModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        NEGOTIATION = "NEGOTIATION", "Under negotiation"
        SIGNED = "SIGNED", "Signed"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        EXPIRED = "EXPIRED", "Expired"
        TERMINATED = "TERMINATED", "Terminated"
        RENEWED = "RENEWED", "Renewed"

    location_prefix = "provider__location"

    provider = models.ForeignKey(
        "location.HealthFacility",
        models.DO_NOTHING,
        related_name="+",
        db_column="HfID",
    )
    empanelment_process = models.ForeignKey(
        "provider_contract.EmpanelmentProcess",
        models.DO_NOTHING,
        related_name="contracts",
        blank=True,
        null=True,
        db_column="EmpanelmentProcessUUID",
    )
    contract_number = models.CharField(
        db_column="ContractNo",
        max_length=30,
        # Deliberately NOT unique. Every amendment and every renewal opens a new
        # version that keeps the same contract_number, so a unique constraint
        # makes replace_object() fail on the second version of any contract.
        # "One open version per contract_number" is enforced in
        # ProviderContractService._assert_single_open_version() instead, which
        # is the portable option (ADR-002).
    )
    status = models.CharField(
        db_column="Status", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    version_no = models.IntegerField(db_column="VersionNo", default=1)
    currency = models.CharField(db_column="Currency", max_length=3, default="USD")
    date_signed = models.DateField(db_column="DateSigned", blank=True, null=True)
    date_start = models.DateField(db_column="DateStart")
    date_end = models.DateField(db_column="DateEnd")
    date_renewal = models.DateField(db_column="DateRenewal", blank=True, null=True)
    notice_period_days = models.IntegerField(
        db_column="NoticePeriodDays", default=60
    )
    auto_renew = models.BooleanField(db_column="AutoRenew", default=False)
    date_terminated = models.DateField(db_column="DateTerminated", blank=True, null=True)
    termination_reason = models.CharField(
        db_column="TerminationReason", max_length=255, blank=True, null=True
    )
    notes = models.CharField(db_column="Notes", max_length=255, blank=True, null=True)

    class Meta:
        managed = True
        db_table = "provider_contract_ProviderContract"
        ordering = ["-date_created"]
        indexes = [
            models.Index(fields=["provider", "status"], name="pce_contract_provider_status"),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(date_end__gte=F("date_start")),
                name="pce_contract_dates_ordered",
            )
        ]

    def __str__(self):
        return f"{self.contract_number} v{self.version_no} {self.get_status_display()}"


class ContractBenefitPackage(ProviderContractBusinessModel):
    location_prefix = "contract__provider__location"

    contract = models.ForeignKey(
        "provider_contract.ProviderContract",
        models.DO_NOTHING,
        related_name="benefit_packages",
        db_column="ContractUUID",
    )
    product = models.ForeignKey(
        "product.Product",
        models.DO_NOTHING,
        related_name="+",
        db_column="ProdID",
    )
    is_included = models.BooleanField(db_column="IsIncluded", default=True)

    class Meta:
        managed = True
        db_table = "provider_contract_ContractBenefitPackage"
        ordering = ["contract_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "product"], name="pce_benefit_package_unique"
            )
        ]

    def __str__(self):
        return f"{self.contract.contract_number} {self.product.code}"


class ContractServiceCategory(ProviderContractBusinessModel):
    """Service scope of a contract, expressed as ``medical.Service`` categories.

    ``medical.Service.category`` is a ``CharField(max_length=1)`` stored in the
    ``ServCategory`` column, not a foreign key: openIMIS has no
    ``ServiceCategory`` entity in the Django models. This model therefore
    mirrors that column rather than inventing a reference to a table that does
    not exist. The human-readable labels live in the legacy
    ``tblServiceCategory`` catalogue and are resolved at the service layer.
    """

    location_prefix = "contract__provider__location"

    contract = models.ForeignKey(
        "provider_contract.ProviderContract",
        models.DO_NOTHING,
        related_name="service_categories",
        db_column="ContractUUID",
    )
    category_code = models.CharField(db_column="ServCategory", max_length=1)

    class Meta:
        managed = True
        db_table = "provider_contract_ContractServiceCategory"
        ordering = ["contract_id", "category_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "category_code"], name="pce_service_category_unique"
            )
        ]

    def __str__(self):
        return f"{self.contract.contract_number} {self.category_code}"


class ContractFeeSchedule(ProviderContractBusinessModel):
    class FeeType(models.TextChoices):
        FFS = "FFS", "Fee for service"
        BUNDLE = "BUNDLE", "Bundle"

    class ResolutionMode(models.TextChoices):
        FACILITY_PRICELIST = "FACILITY_PRICELIST", "Materialized facility pricelist"
        CONTRACT_CALCULE = "CONTRACT_CALCULE", "Resolved by calculation rule"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        CANCELLED = "CANCELLED", "Cancelled"

    location_prefix = "contract__provider__location"

    contract = models.ForeignKey(
        "provider_contract.ProviderContract",
        models.DO_NOTHING,
        related_name="fee_schedules",
        db_column="ContractUUID",
    )
    name = models.CharField(db_column="Name", max_length=100)
    fee_type = models.CharField(
        db_column="FeeType", max_length=10, choices=FeeType.choices, default=FeeType.FFS
    )
    status = models.CharField(
        db_column="Status", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    currency = models.CharField(db_column="Currency", max_length=3, default="USD")
    resolution_mode = models.CharField(
        db_column="ResolutionMode",
        max_length=20,
        choices=ResolutionMode.choices,
        default=ResolutionMode.FACILITY_PRICELIST,
    )
    services_pricelist = models.ForeignKey(
        "medical_pricelist.ServicesPricelist",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="PLServiceID",
    )
    items_pricelist = models.ForeignKey(
        "medical_pricelist.ItemsPricelist",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="PLItemID",
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeSchedule"
        ordering = ["contract_id", "name"]
        indexes = [
            models.Index(fields=["contract", "status"], name="pce_fee_sched_contract_status"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_fee_type_display()})"


class ContractFeeItem(ProviderContractBusinessModel):
    location_prefix = "fee_schedule__contract__provider__location"

    fee_schedule = models.ForeignKey(
        "provider_contract.ContractFeeSchedule",
        models.DO_NOTHING,
        related_name="fee_items",
        db_column="FeeScheduleUUID",
    )
    service = models.ForeignKey(
        "medical.Service",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ServiceID",
    )
    item = models.ForeignKey(
        "medical.Item",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ItemID",
    )
    amount = models.DecimalField(
        db_column="Amount", max_digits=18, decimal_places=2
    )
    quantity = models.IntegerField(db_column="Quantity", default=1)
    is_included = models.BooleanField(db_column="IsIncluded", default=True)
    overrule_max_amount = models.DecimalField(
        db_column="OverruleMaxAmount",
        max_digits=18,
        decimal_places=2,
        blank=True,
        null=True,
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeItem"
        ordering = ["fee_schedule_id"]
        indexes = [
            models.Index(fields=["fee_schedule", "service"], name="pce_fee_item_schedule_service"),
            models.Index(fields=["fee_schedule", "item"], name="pce_fee_item_schedule_item"),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(service__isnull=False, item__isnull=True)
                    | Q(service__isnull=True, item__isnull=False)
                ),
                name="pce_fee_item_single_target",
            ),
            models.CheckConstraint(
                check=Q(amount__gte=Decimal("0")),
                name="pce_fee_item_amount_positive",
            ),
        ]

    def __str__(self):
        target = self.service or self.item
        return f"{target} {self.amount}"


class ContractFeeBulkOperation(ProviderContractModel):
    class OperationType(models.TextChoices):
        CREATE = "CREATE", "Create"
        AMEND = "AMEND", "Amend"
        DEACTIVATE = "DEACTIVATE", "Deactivate"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partially completed"
        FAILED = "FAILED", "Failed"

    location_prefix = "contract__provider__location"

    contract = models.ForeignKey(
        "provider_contract.ProviderContract",
        models.DO_NOTHING,
        related_name="bulk_operations",
        db_column="ContractUUID",
    )
    operation_type = models.CharField(
        db_column="OperationType", max_length=20, choices=OperationType.choices
    )
    status = models.CharField(
        db_column="Status", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    filename = models.CharField(db_column="FileName", max_length=255)
    total_rows = models.IntegerField(db_column="TotalRows", default=0)
    succeeded_rows = models.IntegerField(db_column="SucceededRows", default=0)
    failed_rows = models.IntegerField(db_column="FailedRows", default=0)
    date_started = models.DateTimeField(
        db_column="DateStarted", blank=True, null=True
    )
    date_finished = models.DateTimeField(
        db_column="DateFinished", blank=True, null=True
    )
    error_report = models.CharField(
        db_column="ErrorReport", max_length=255, blank=True, null=True
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeBulkOperation"
        ordering = ["-date_created"]

    def __str__(self):
        return f"{self.filename} {self.get_status_display()}"


class ClaimScopeViolation(ProviderContractModel):
    """Advisory record of a failed empanelment or pricing check.

    Writing this row is the *only* effect a negative gate verdict has. The
    claim itself is never rejected, cancelled, amended or deleted (ADR-006).
    Migrated in its own batch because it holds foreign keys into ``claim``.
    """

    class ReasonCode(models.TextChoices):
        NOT_EMPANELLED = "NOT_EMPANELLED", "Provider not empanelled"
        OUTSIDE_SCOPE = "OUTSIDE_SCOPE", "Service outside contracted scope"
        CONTRACT_EXPIRED = "CONTRACT_EXPIRED", "Contract expired at date of service"
        NO_ACTIVE_CONTRACT = "NO_ACTIVE_CONTRACT", "No active contract on that date"
        LICENSE_EXPIRED = "LICENSE_EXPIRED", "Licence or accreditation expired"
        NO_FEE_MATCH = "NO_FEE_MATCH", "No matching fee for this service"
        FEE_MISMATCH = "FEE_MISMATCH", "Claimed amount differs from contract fee"

    class Severity(models.TextChoices):
        INFO = "INFO", "Information"
        WARNING = "WARNING", "Warning"
        BLOCKING = "BLOCKING", "Blocking"

    location_prefix = "provider__location"

    claim = models.ForeignKey(
        "claim.Claim",
        models.DO_NOTHING,
        related_name="provider_contract_violations",
        db_column="ClaimID",
    )
    claim_service = models.ForeignKey(
        "claim.ClaimService",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ClaimServiceID",
    )
    provider = models.ForeignKey(
        "location.HealthFacility",
        models.DO_NOTHING,
        related_name="+",
        db_column="HfID",
    )
    contract = models.ForeignKey(
        "provider_contract.ProviderContract",
        models.DO_NOTHING,
        related_name="violations",
        blank=True,
        null=True,
        db_column="ContractUUID",
    )
    service_code = models.CharField(db_column="ServCode", max_length=6)
    date_of_service = models.DateTimeField(
        db_column="DateOfService", blank=True, null=True
    )
    reason_code = models.CharField(
        db_column="ReasonCode", max_length=20, choices=ReasonCode.choices
    )
    severity = models.CharField(
        db_column="Severity", max_length=10, choices=Severity.choices, default=Severity.WARNING
    )
    details = models.JSONField(db_column="Details", blank=True, null=True)
    is_resolved = models.BooleanField(db_column="IsResolved", default=False)
    reviewed_by = models.ForeignKey(
        "core.User",
        models.DO_NOTHING,
        related_name="+",
        blank=True,
        null=True,
        db_column="ReviewedByUUID",
    )
    date_resolved = models.DateTimeField(
        db_column="DateResolved", blank=True, null=True
    )
    resolution = models.CharField(
        db_column="Resolution", max_length=255, blank=True, null=True
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ClaimScopeViolation"
        ordering = ["-date_created"]
        indexes = [
            models.Index(fields=["claim", "reason_code"], name="pce_violation_claim_reason"),
            models.Index(fields=["provider", "date_of_service"], name="pce_violation_provider_date"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["claim", "claim_service", "reason_code"],
                name="pce_violation_unique",
            )
        ]

    def __str__(self):
        return f"{self.claim_id} {self.reason_code}"


class EmpanelmentWorkflowMutation(core_models.UUIDModel, core_models.ObjectMutation):
    workflow = models.ForeignKey(
        EmpanelmentWorkflow, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_workflows"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentWorkflowMutation"


class EmpanelmentStageMutation(core_models.UUIDModel, core_models.ObjectMutation):
    stage = models.ForeignKey(
        EmpanelmentStage, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_stages"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentStageMutation"


class EmpanelmentProcessMutation(core_models.UUIDModel, core_models.ObjectMutation):
    process = models.ForeignKey(
        EmpanelmentProcess, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_processes"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentProcessMutation"


class EmpanelmentStageTransitionMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    transition = models.ForeignKey(
        EmpanelmentStageTransition, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_stage_transitions"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentStageTransitionMutation"


class EmpanelmentDocumentTypeMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    document_type = models.ForeignKey(
        EmpanelmentDocumentType, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_document_types"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentDocumentTypeMutation"


class EmpanelmentProcessDocumentMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    document = models.ForeignKey(
        EmpanelmentProcessDocument, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="empanelment_process_documents"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_EmpanelmentProcessDocumentMutation"


class ProviderContractMutation(core_models.UUIDModel, core_models.ObjectMutation):
    contract = models.ForeignKey(
        ProviderContract, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="provider_contracts"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ProviderContractMutation"


class ContractBenefitPackageMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    benefit_package = models.ForeignKey(
        ContractBenefitPackage, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="contract_benefit_packages"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractBenefitPackageMutation"


class ContractServiceCategoryMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    service_category = models.ForeignKey(
        ContractServiceCategory, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="contract_service_categories"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractServiceCategoryMutation"


class ContractFeeScheduleMutation(core_models.UUIDModel, core_models.ObjectMutation):
    fee_schedule = models.ForeignKey(
        ContractFeeSchedule, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="contract_fee_schedules"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeScheduleMutation"


class ContractFeeItemMutation(core_models.UUIDModel, core_models.ObjectMutation):
    fee_item = models.ForeignKey(
        ContractFeeItem, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="contract_fee_items"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeItemMutation"


class ContractFeeBulkOperationMutation(
    core_models.UUIDModel, core_models.ObjectMutation
):
    bulk_operation = models.ForeignKey(
        ContractFeeBulkOperation, models.DO_NOTHING, related_name="mutations"
    )
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name="contract_fee_bulk_operations"
    )

    class Meta:
        managed = True
        db_table = "provider_contract_ContractFeeBulkOperationMutation"
