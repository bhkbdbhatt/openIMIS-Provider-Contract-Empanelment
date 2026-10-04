"""Shared fixtures for the provider_contract test suite.

Every test in this package needs the same three things: an actor to satisfy the
non-nullable ``user_created`` / ``user_updated`` audit columns, a health
facility to hang contracts off, and the reference data that
``location.HealthFacility`` requires. They are built here once so the tests read
as behaviour rather than as setup.

Note the ``save(user=...)`` idiom used throughout. ``HistoryModel.save()`` calls
``get_user()``, which -- when given neither ``user`` nor ``username`` -- silently
falls back to ``User.objects.filter(i_user_id=1)``, and then *overwrites*
``user_created`` with whatever it found (possibly ``None``, which then violates
the NOT NULL constraint). Passing ``user_created`` to ``objects.create()`` is
therefore not enough; the user has to be passed to ``save()``.

Fixture builders come from the module test helpers that ship with openIMIS:

* ``core.test_helpers.create_test_technical_user``
* ``location.test_helpers.create_test_health_facility``

which is the point: the suite must not invent its own way of building an
openIMIS user or facility, or it would stop being a test of this module and
start being a test of a private fixture dialect.
"""

from django.test import TestCase

from core.test_helpers import create_test_technical_user
from location.test_helpers import (
    create_test_health_facility,
    create_test_location,
)

from provider_contract.models import (
    ContractFeeItem,
    ContractFeeSchedule,
    EmpanelmentDocumentType,
    EmpanelmentProcess,
    EmpanelmentStage,
    EmpanelmentWorkflow,
    ProviderContract,
)


def create_district_with_parent(code):
    """Build a district that openIMIS will actually grant access for.

    ``UserDistrict.get_user_districts`` filters on
    ``location__parent__isnull=False``, so a district created with a bare
    ``create_test_location("D")`` never reaches the caller's allowed-location
    list and row-security tests silently see an empty queryset -- which looks
    like "isolation works" even when nothing was actually filtered. Every
    district used for scoping therefore needs a region above it.
    """
    region = create_test_location("R", custom_props={"code": code + "-R"})
    return create_test_location(
        "D", custom_props={"code": code, "parent": region}
    )


class ProviderContractTestCase(TestCase):
    """Base case providing an actor and a health facility."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = create_test_technical_user(username="pce_test_admin", super_user=True)
        cls.health_facility = create_test_health_facility(code="PCE-HF-1")

    def _save(self, instance):
        instance.save(user=self.user)
        return instance

    def build_workflow(self, code="DEFAULT", **kwargs):
        return self._save(
            EmpanelmentWorkflow(
                code=code,
                name=kwargs.pop("name", "Standard provider empanelment"),
                is_default=kwargs.pop("is_default", True),
                **kwargs,
            )
        )

    def build_stage(self, workflow, code="APPLICATION", **kwargs):
        return self._save(
            EmpanelmentStage(
                workflow=workflow,
                code=code,
                name=kwargs.pop("name", "Application received"),
                sequence=kwargs.pop("sequence", 1),
                **kwargs,
            )
        )

    def build_process(self, provider=None, workflow=None, **kwargs):
        return self._save(
            EmpanelmentProcess(
                provider=provider or self.health_facility,
                workflow=workflow or self.build_workflow(),
                reference_no=kwargs.pop("reference_no", "PCE-REF-1"),
                **kwargs,
            )
        )

    def build_contract(self, provider=None, **kwargs):
        return self._save(
            ProviderContract(
                provider=provider or self.health_facility,
                contract_number=kwargs.pop("contract_number", "PC-2026-001"),
                date_start=kwargs.pop("date_start", "2026-01-01"),
                date_end=kwargs.pop("date_end", "2026-12-31"),
                **kwargs,
            )
        )

    def build_fee_schedule(self, contract=None, **kwargs):
        return self._save(
            ContractFeeSchedule(
                contract=contract or self.build_contract(),
                name=kwargs.pop("name", "Standard FFS schedule"),
                **kwargs,
            )
        )

    def build_fee_item(self, fee_schedule=None, service=None, **kwargs):
        return self._save(
            ContractFeeItem(
                fee_schedule=fee_schedule or self.build_fee_schedule(),
                service=service,
                amount=kwargs.pop("amount", "25.00"),
                **kwargs,
            )
        )

    def make_document_type(self, code="LICENCE"):
        return self._save(
            EmpanelmentDocumentType(code=code, name="Operating licence")
        )
