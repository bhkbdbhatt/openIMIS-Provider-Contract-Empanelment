"""Empanelment workflow execution.

A process walks the stages of its workflow in ``sequence`` order. Each move is
recorded as an ``EmpanelmentStageTransition`` so the audit trail shows which
stage a facility was in when a decision was taken -- the thing a scheme needs
when an empanelment is challenged months later.

The stage catalogue itself (``EmpanelmentWorkflow`` / ``EmpanelmentStage`` /
``EmpanelmentDocumentType``) is reference data, seeded by
``seed_empanelment_catalogue``. What lives here is the *execution* of a process
against that catalogue.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from location.models import HealthFacility

from core.signals import register_service_signal

from provider_contract.models import (
    EmpanelmentDocumentType,
    EmpanelmentProcess,
    EmpanelmentProcessDocument,
    EmpanelmentStage,
    EmpanelmentStageTransition,
    EmpanelmentWorkflow,
)

from .common import as_date, as_datetime


class EmpanelmentService:
    def __init__(self, user):
        self.user = user

    # ------------------------------------------------------------------ reads

    def get_process(self, process_uuid, for_update=False):
        queryset = EmpanelmentProcess.filter_queryset(
            EmpanelmentProcess.objects.all()
        )
        if for_update:
            queryset = queryset.select_for_update()
        process = queryset.filter(uuid=process_uuid).first()
        if process is None:
            raise ValidationError(
                f"Empanelment process {process_uuid} not found or not visible."
            )
        return process

    def _resolve_provider(self, provider_id):
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

    def _open_process(self, provider):
        """Only one live application per facility at a time.

        Two concurrent open applications for the same facility would let the
        committee approve two contracts, which the contract service would then
        have to arbitrate. Cheaper to refuse the second application.
        """
        existing = EmpanelmentProcess.objects.filter(
            provider=provider,
            status__in=[
                EmpanelmentProcess.Status.DRAFT,
                EmpanelmentProcess.Status.SUBMITTED,
                EmpanelmentProcess.Status.UNDER_REVIEW,
            ],
        ).first()
        if existing:
            raise ValidationError(
                f"This facility already has an open application ({existing.reference_no})."
            )

    def _stages(self, workflow):
        """The workflow's stages in sequence order.

        Returns a **list**, not a queryset: callers index and iterate it, and a
        queryset would defer the ordering to query time for no benefit. Use
        ``self._stages(w)[0]`` or ``next_stage`` rather than ``.first()`` on the
        result.
        """
        return list(
            EmpanelmentStage.objects.filter(workflow=workflow).order_by("sequence")
        )

    def _next_stage(self, process):
        if process.current_stage_id is None:
            stages = self._stages(process.workflow)
            return stages[0] if stages else None
        following = (
            EmpanelmentStage.objects.filter(
                workflow=process.workflow,
                sequence__gt=process.current_stage.sequence,
            )
            .order_by("sequence")
            .first()
        )
        return following

    def _document_gate(self, process, stage):
        """Enforce ``requires_all_documents`` before entering ``stage``."""
        if not stage.requires_all_documents:
            return
        outstanding = self._outstanding_documents(process)
        if outstanding:
            raise ValidationError(
                f"Stage {stage.code} requires every mandatory document. Outstanding: "
                + ", ".join(sorted(outstanding))
            )

    def _outstanding_documents(self, process):
        """Mandatory document types not yet VERIFIED on this process.

        Scoped per *process*, not per facility: a licence verified for last
        year's application is not evidence for this one, because it may since
        have lapsed.
        """
        verified = set(
            EmpanelmentProcessDocument.objects.filter(
                process=process, status=EmpanelmentProcessDocument.Status.VERIFIED
            ).values_list("document_type__code", flat=True)
        )
        mandatory = set(
            EmpanelmentDocumentType.objects.filter(is_mandatory=True).values_list(
                "code", flat=True
            )
        )
        return mandatory - verified

    # ------------------------------------------------------------ transitions

    @transaction.atomic
    @register_service_signal("provider_contract_service.create_empanelment_process")
    def create(self, data):
        provider = self._resolve_provider(data.get("provider_id"))
        workflow = data["workflow"]
        self._assert_workflow_usable(workflow)
        self._open_process(provider)

        process = EmpanelmentProcess(
            provider=provider,
            workflow=workflow,
            reference_no=data["reference_no"],
            status=EmpanelmentProcess.Status.DRAFT,
            valid_until=as_date(data.get("valid_until")),
        )
        process.save(user=self.user)
        return process

    def _assert_workflow_usable(self, workflow):
        if workflow.status != EmpanelmentWorkflow.Status.ACTIVE:
            raise ValidationError(
                f"Workflow {workflow.code} is {workflow.get_status_display()} and "
                "cannot accept new applications."
            )
        if not self._stages(workflow):
            raise ValidationError(
                f"Workflow {workflow.code} has no stages. Run "
                "manage.py seed_empanelment_catalogue."
            )

    @transaction.atomic
    @register_service_signal("provider_contract_service.submit_empanelment_process")
    def submit(self, process):
        """DRAFT -> SUBMITTED, entering the workflow's first stage."""
        self._assert_status(process, EmpanelmentProcess.Status.DRAFT)
        stages = self._stages(process.workflow)
        if not stages:
            raise ValidationError("The workflow has no stages to submit into.")
        first = stages[0]

        process.status = EmpanelmentProcess.Status.SUBMITTED
        process.date_submitted = timezone.now()
        process.current_stage = first
        process.save(user=self.user)
        self._record(process, from_stage=None, to_stage=first)
        return process

    @transaction.atomic
    @register_service_signal("provider_contract_service.advance_empanelment_process")
    def advance(self, process, outcome=EmpanelmentStageTransition.Outcome.PASSED,
                comment=None, decision_stage_result=None):
        """Move to the next stage of the workflow.

        The current stage is recorded with its outcome before moving on, so the
        transition log is a complete history rather than a list of positions.
        On the last stage the process is left UNDER_REVIEW and must be decided
        explicitly: reaching the committee is not the same as being approved.
        """
        self._assert_status(
            process,
            EmpanelmentProcess.Status.SUBMITTED,
            EmpanelmentProcess.Status.UNDER_REVIEW,
        )
        if process.current_stage_id is None:
            raise ValidationError("The process has no current stage to advance from.")

        current = process.current_stage
        if outcome == EmpanelmentStageTransition.Outcome.FAILED:
            # A failed intermediate stage short-circuits to rejection rather than
            # advancing; continuing past a failed document or site check would
            # let an unvetted provider reach the committee.
            return self._reject(process, comment or f"Stage {current.code} failed.")

        nxt = self._next_stage(process)
        if nxt is None:
            process.status = EmpanelmentProcess.Status.UNDER_REVIEW
            process.save(user=self.user)
            self._record(
                process,
                from_stage=current,
                to_stage=None,
                outcome=outcome,
                comment=comment,
            )
            return process

        self._document_gate(process, nxt)
        process.status = EmpanelmentProcess.Status.UNDER_REVIEW
        process.current_stage = nxt
        process.save(user=self.user)
        self._record(
            process, from_stage=current, to_stage=nxt, outcome=outcome, comment=comment
        )
        if decision_stage_result is not None:
            return self._apply_decision(
                process, approved=bool(decision_stage_result), reason=comment
            )
        return process

    @transaction.atomic
    @register_service_signal("provider_contract_service.decide_empanelment_process")
    def decide(self, process, approved, reason=None):
        """Record the committee's decision and close the process."""
        self._assert_status(
            process,
            EmpanelmentProcess.Status.SUBMITTED,
            EmpanelmentProcess.Status.UNDER_REVIEW,
        )
        return self._apply_decision(process, approved=approved, reason=reason)

    def _apply_decision(self, process, approved, reason=None):
        process.status = (
            EmpanelmentProcess.Status.APPROVED
            if approved
            else EmpanelmentProcess.Status.REJECTED
        )
        process.date_decided = timezone.now()
        process.decided_by = self.user
        process.decision_reason = reason
        process.save(user=self.user)
        self._record(
            process,
            from_stage=process.current_stage,
            to_stage=None,
            outcome=(
                EmpanelmentStageTransition.Outcome.PASSED
                if approved
                else EmpanelmentStageTransition.Outcome.FAILED
            ),
            comment=reason,
        )
        return process

    def _reject(self, process, reason):
        process.status = EmpanelmentProcess.Status.REJECTED
        process.date_decided = timezone.now()
        process.decided_by = self.user
        process.decision_reason = reason
        process.save(user=self.user)
        self._record(
            process,
            from_stage=process.current_stage,
            to_stage=None,
            outcome=EmpanelmentStageTransition.Outcome.FAILED,
            comment=reason,
        )
        return process

    @transaction.atomic
    @register_service_signal("provider_contract_service.withdraw_empanelment_process")
    def withdraw(self, process, reason=None):
        """Withdraw an application the provider no longer wants to pursue."""
        self._assert_status(
            process,
            EmpanelmentProcess.Status.DRAFT,
            EmpanelmentProcess.Status.SUBMITTED,
            EmpanelmentProcess.Status.UNDER_REVIEW,
        )
        process.status = EmpanelmentProcess.Status.WITHDRAWN
        process.decision_reason = reason
        process.save(user=self.user)
        self._record(
            process,
            from_stage=process.current_stage,
            to_stage=None,
            outcome=EmpanelmentStageTransition.Outcome.SKIPPED,
            comment=reason,
        )
        return process

    # -------------------------------------------------------------- documents

    @transaction.atomic
    @register_service_signal("provider_contract_service.add_empanelment_document")
    def add_document(self, process, document_type, data):
        document = EmpanelmentProcessDocument(
            process=process,
            document_type=document_type,
            status=EmpanelmentProcessDocument.Status.UPLOADED,
            file_reference=data.get("file_reference"),
            date_uploaded=as_datetime(data.get("date_uploaded")) or timezone.now(),
            date_expires=as_date(data.get("date_expires")),
        )
        document.save(user=self.user)
        return document

    @transaction.atomic
    @register_service_signal(
        "provider_contract_service.verify_empanelment_document"
    )
    def verify_document(self, document, note=None):
        self._assert_document_status(
            document,
            EmpanelmentProcessDocument.Status.UPLOADED,
            EmpanelmentProcessDocument.Status.REJECTED,
        )
        if document.document_type.requires_expiry and not document.date_expires:
            raise ValidationError(
                f"{document.document_type.code} requires an expiry date."
            )
        document.status = EmpanelmentProcessDocument.Status.VERIFIED
        document.reviewed_by = self.user
        document.date_reviewed = timezone.now()
        document.review_note = note
        document.save(user=self.user)
        return document

    @transaction.atomic
    @register_service_signal(
        "provider_contract_service.reject_empanelment_document"
    )
    def reject_document(self, document, note):
        if not note:
            raise ValidationError("A rejection note is required.")
        document.status = EmpanelmentProcessDocument.Status.REJECTED
        document.reviewed_by = self.user
        document.date_reviewed = timezone.now()
        document.review_note = note
        document.save(user=self.user)
        return document

    @transaction.atomic
    @register_service_signal("provider_contract_service.delete_empanelment_process")
    def delete(self, process):
        """Soft-delete a draft application."""
        if process.status != EmpanelmentProcess.Status.DRAFT:
            raise ValidationError(
                "Only a DRAFT application can be deleted. Withdraw it instead so "
                "the history is preserved."
            )
        process.delete(user=self.user)
        return process

    # ----------------------------------------------------------------- helper

    def _record(self, process, from_stage=None, to_stage=None, outcome=None, comment=None):
        transition = EmpanelmentStageTransition(
            process=process,
            from_stage=from_stage,
            to_stage=to_stage,
            outcome=outcome or EmpanelmentStageTransition.Outcome.PASSED,
            transition_date=timezone.now(),
            comment=comment,
        )
        transition.save(user=self.user)
        return transition

    @staticmethod
    def _assert_status(process, *allowed):
        if process.status not in allowed:
            names = ", ".join(
                c.label for c in EmpanelmentProcess.Status if c.value in allowed
            )
            raise ValidationError(
                f"Application {process.reference_no} is "
                f"{process.get_status_display()}; expected one of: {names}."
            )

    @staticmethod
    def _assert_document_status(document, *allowed):
        if document.status not in allowed:
            raise ValidationError(
                f"Document is {document.get_status_display()}; expected one of: "
                + ", ".join(
                    c.label
                    for c in EmpanelmentProcessDocument.Status
                    if c.value in allowed
                )
            )
