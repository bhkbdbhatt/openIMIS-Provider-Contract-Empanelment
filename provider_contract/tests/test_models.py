"""Model-level invariants that a static check cannot prove.

These are the properties that, if they silently regress, corrupt financial
history rather than merely failing a request: the UUID primary key contract,
soft delete, effective dating, and the constraints that keep a fee row pointing
at exactly one billable thing.
"""

from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import IntegrityError
from django.test import TestCase

from medical.test_helpers import create_test_service

from provider_contract import models as pce_models
from provider_contract.models import (
    ContractFeeSchedule,
    EmpanelmentStage,
    ProviderContract,
    ProviderContractMutation,
)

from .base import ProviderContractTestCase


class UuidPrimaryKeyTests(ProviderContractTestCase):
    """``uuid`` is a property aliasing ``id``; it must not be a column."""

    def test_uuid_is_not_a_field(self):
        field_names = {f.name for f in ProviderContract._meta.get_fields()}
        self.assertNotIn(
            "uuid", field_names, "declaring a uuid field shadows HistoryModel.uuid"
        )

    def test_uuid_property_mirrors_id(self):
        contract = self.build_contract()
        self.assertEqual(contract.uuid, contract.id)

    def test_pk_is_assigned_on_save(self):
        contract = self.build_contract()
        self.assertIsNotNone(contract.pk)
        self.assertTrue(ProviderContract.objects.filter(id=contract.pk).exists())


class SoftDeleteTests(ProviderContractTestCase):
    """HistoryModel.delete() is a soft delete; nothing is ever removed."""

    def test_delete_marks_is_deleted_instead_of_removing_the_row(self):
        contract = self.build_contract()
        contract.delete(user=self.user)

        self.assertTrue(
            ProviderContract.objects.filter(id=contract.id).exists(),
            "row was hard-deleted; HistoryModel.delete() must only set is_deleted",
        )
        self.assertTrue(contract.is_deleted)

    def test_filter_queryset_hides_soft_deleted_rows(self):
        contract = self.build_contract()
        contract.delete(user=self.user)

        live = ProviderContract.filter_queryset(ProviderContract.objects.all())
        self.assertNotIn(contract, live)


class ValidityTests(ProviderContractTestCase):
    """Business models carry their own filter_validity implementation."""

    def test_filter_validity_includes_an_open_row(self):
        contract = self.build_contract(
            date_valid_from="2020-01-01", date_valid_to=None
        )
        self.assertIn(
            contract, ProviderContract.filter_validity(date=datetime(2026, 6, 1))
        )

    def test_filter_validity_excludes_a_closed_row(self):
        contract = self.build_contract(
            date_valid_from="2020-01-01", date_valid_to="2021-01-01"
        )
        self.assertNotIn(
            contract, ProviderContract.filter_validity(date=datetime(2026, 6, 1))
        )
        self.assertIn(
            contract, ProviderContract.filter_validity(date=datetime(2020, 6, 1))
        )

    def test_filter_validity_is_overridden_on_every_business_model(self):
        """Guards the reason the override exists at all.

        The inherited ``OpenIMISHistoryMixin.filter_validity`` filters on
        ``active`` and ``date_deactivated``, columns that belong to
        ``OpenIMISModel`` and not to ``HistoryModel``. Our override accepts and
        ignores those kwargs, so a regression here would not raise -- it would
        silently return the wrong rows. Assert on the resolved function.
        """
        expected = pce_models.ProviderContractBusinessModel.filter_validity.__func__
        for model in (ProviderContract, ContractFeeSchedule, EmpanelmentStage):
            with self.subTest(model=model.__name__):
                self.assertIs(model.filter_validity.__func__, expected)


class ConstraintTests(ProviderContractTestCase):
    """Portable constraints -- no partial indexes, so the DB enforces these."""

    def assert_integrity_error(self, build):
        """The insert must be refused, however openIMIS chooses to refuse it.

        ``core.validation.base`` hooks ``pre_save`` and turns a failed CHECK or
        UNIQUE into a ``ValidationError`` naming the constraint, so the DB may
        never see the statement. Asserting only ``IntegrityError`` would pass
        vacuously if the validator were ever removed, and would fail for the
        wrong reason while it is there.
        """
        # The savepoint keeps the test transaction usable after the failure.
        with self.assertRaises((IntegrityError, ValidationError)), transaction.atomic():
            build()

    def test_contract_number_may_repeat_across_versions(self):
        """Amendments keep the number, so the DB cannot forbid duplicates.

        The "one open version per contract_number" rule lives in
        ProviderContractService._assert_single_open_version() (ADR-002); this
        test pins the fact that the column itself is deliberately not unique,
        because making it so breaks replace_object() on the second version.
        """
        self.build_contract(contract_number="PC-DUP")
        self.build_contract(contract_number="PC-DUP", date_start="2027-01-01",
                            date_end="2027-12-31")
        self.assertEqual(
            ProviderContract.objects.filter(contract_number="PC-DUP").count(), 2
        )

    def test_date_end_must_not_precede_date_start(self):
        self.assert_integrity_error(
            lambda: self.build_contract(date_start="2026-12-31", date_end="2026-01-01")
        )

    def test_stage_code_is_unique_per_workflow(self):
        workflow = self.build_workflow()
        self.build_stage(workflow, code="SITE_VISIT")
        self.assert_integrity_error(
            lambda: self.build_stage(workflow, code="SITE_VISIT")
        )

    def test_stage_code_may_repeat_across_workflows(self):
        self.build_stage(self.build_workflow(code="W1"), code="SITE_VISIT")
        self.build_stage(self.build_workflow(code="W2"), code="SITE_VISIT")
        self.assertEqual(
            EmpanelmentStage.objects.filter(code="SITE_VISIT").count(), 2
        )

    def test_fee_item_amount_must_not_be_negative(self):
        self.assert_integrity_error(
            lambda: self.build_fee_item(amount=Decimal("-1.00"))
        )

    def test_fee_item_target_must_be_exactly_one_of_service_or_item(self):
        # Neither target set: violates pce_fee_item_single_target.
        self.assert_integrity_error(lambda: self.build_fee_item(service=None))

    def test_fee_item_accepts_a_service_target(self):
        service = create_test_service(category="S")
        item = self.build_fee_item(service=service, amount=Decimal("30.00"))
        item.refresh_from_db()
        self.assertEqual(item.service_id, service.id)


class MutationLogWiringTests(ProviderContractTestCase):
    """Each exposed entity has a companion row-log model."""

    def test_mutation_model_points_at_contract_and_mutation_log(self):
        field_names = {f.name for f in ProviderContractMutation._meta.get_fields()}
        self.assertIn("contract", field_names)
        self.assertIn("mutation", field_names)

    def test_mutation_related_names(self):
        self.assertEqual(
            ProviderContractMutation._meta.get_field("contract")
            .remote_field.related_name,
            "mutations",
        )
        self.assertEqual(
            ProviderContractMutation._meta.get_field("mutation")
            .remote_field.related_name,
            "provider_contracts",
        )


class ConfigurationTests(TestCase):
    """Behaviour flags must have safe defaults so the module runs unconfigured."""

    def test_gate_defaults_to_flag_only(self):
        from provider_contract.apps import DEFAULT_CFG

        self.assertTrue(DEFAULT_CFG["pce_gate_enabled"])
        self.assertEqual(DEFAULT_CFG["pce_gate_mode"], "flag_only")

    def test_fee_resolution_default(self):
        from provider_contract.apps import DEFAULT_CFG

        self.assertEqual(
            DEFAULT_CFG["pce_fee_resolution_mode"], "FACILITY_PRICELIST"
        )


class SeedCommandRegistrationTests(TestCase):
    """The seed command is the sanctioned way to create the catalogue."""

    def test_seed_command_is_registered(self):
        from django.core.management import get_commands

        self.assertIn("seed_empanelment_catalogue", get_commands())
