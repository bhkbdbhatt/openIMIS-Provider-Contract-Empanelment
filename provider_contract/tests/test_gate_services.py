"""Claims-gate tests.

The gate is advisory (ADR-006), so the most important assertions in this file
are negative: on every failure branch, the claim must be **unchanged**. A gate
test that only checks the returned verdict is incomplete, because the failure
mode being guarded against is precisely a gate that quietly edits claims.

Only one side effect is permitted: a ``ClaimScopeViolation`` row.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import override_settings

from claim.test_helpers import create_test_claim, create_test_claimservice
from medical.test_helpers import create_test_service

from provider_contract.models import (
    ClaimScopeViolation,
    ContractFeeSchedule,
    ProviderContract,
)
from provider_contract.services import (
    ClaimScopeViolationService,
    applicable_fee,
    check_provider_empanelment,
    get_config,
)

from .base import ProviderContractTestCase


def _claim_state(claim):
    """Every concrete field of a claim, for before/after comparison."""
    return {
        f.attname: getattr(claim, f.attname)
        for f in claim._meta.fields
        if not f.primary_key
    }


class GateVerdictTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.service = create_test_service(category="S")

    def make_active_contract(self, number="PC-G1", **kwargs):
        # The gate is asked about a service rendered on 2026-06-01, so the
        # version has to have been in force then. ValidityMixin.date_valid_from
        # defaults to now(), and a version recorded today correctly refuses to
        # cover a date that precedes it.
        kwargs.setdefault("date_valid_from", datetime(2025, 12, 1))
        contract = self.build_contract(contract_number=number, **kwargs)
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        return contract

    def test_no_contract_at_all_is_not_empanelled(self):
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.assertFalse(verdict["empanelled"])
        self.assertEqual(
            verdict["reason_code"], ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT
        )
        self.assertEqual(verdict["severity"], ClaimScopeViolation.Severity.BLOCKING)

    def test_missing_provider_is_blocking(self):
        verdict = check_provider_empanelment(None)
        self.assertFalse(verdict["empanelled"])
        self.assertEqual(
            verdict["reason_code"], ClaimScopeViolation.ReasonCode.NOT_EMPANELLED
        )

    def test_draft_contract_does_not_count_as_empanelled(self):
        self.build_contract(contract_number="PC-G2")
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.assertFalse(verdict["empanelled"])

    def test_active_contract_is_empanelled(self):
        self.make_active_contract()
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.assertTrue(verdict["empanelled"])
        self.assertEqual(verdict["contract"], "PC-G1")
        self.assertIsNone(verdict["reason_code"])

    def test_terminated_contract_is_not_empanelled(self):
        contract = self.make_active_contract()
        contract.status = ProviderContract.Status.TERMINATED
        contract.save(user=self.user)
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.assertFalse(verdict["empanelled"])

    def test_service_outside_contracted_scope_is_a_warning(self):
        from provider_contract.models import ContractServiceCategory

        contract = self.make_active_contract()
        # "A" (ambulatory) is deliberately not the service's own category.
        ContractServiceCategory(contract=contract, category_code="A").save(
            user=self.user
        )

        verdict = check_provider_empanelment(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )

        self.assertFalse(verdict["empanelled"])
        self.assertEqual(
            verdict["reason_code"], ClaimScopeViolation.ReasonCode.OUTSIDE_SCOPE
        )
        # A scope miss is a warning, not a blocker: the claim is still payable.
        self.assertEqual(verdict["severity"], ClaimScopeViolation.Severity.WARNING)

    def test_contract_without_scope_rows_covers_everything(self):
        """'No scope recorded' must not read as 'out of scope' (migrated data)."""
        self.make_active_contract()
        verdict = check_provider_empanelment(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertTrue(verdict["empanelled"])

    def test_service_within_recorded_scope_is_empanelled(self):
        from provider_contract.models import ContractServiceCategory

        contract = self.make_active_contract()
        category = ContractServiceCategory(
            contract=contract, category_code=self.service.category
        )
        category.save(user=self.user)

        verdict = check_provider_empanelment(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertTrue(verdict["empanelled"])


class ApplicableFeeTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.service = create_test_service(category="S")

    def active_with_fee(self, amount="42.00"):
        # Backdated for the same reason as GateVerdictTests.make_active_contract:
        # the fee is looked up for a service rendered on 2026-06-01, so the
        # contract version has to have been in force then.
        contract = self.build_contract(
            contract_number="PC-F1", date_valid_from=datetime(2025, 12, 1)
        )
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        # The schedule needs the same treatment: effective_fee_schedule()
        # resolves the schedule in force at the service date too.
        schedule = self.build_fee_schedule(
            contract=contract, date_valid_from=datetime(2025, 12, 1)
        )
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)
        self.build_fee_item(fee_schedule=schedule, service=self.service, amount=amount)
        return contract

    def test_fee_is_resolved_for_an_active_contract(self):
        self.active_with_fee()
        result = applicable_fee(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertEqual(Decimal(result["amount"]), Decimal("42.00"))
        self.assertEqual(result["contract"], "PC-F1")

    def test_result_states_which_mechanism_priced_the_claim(self):
        """Otherwise materialized and calcrule prices are indistinguishable."""
        self.active_with_fee()
        result = applicable_fee(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertEqual(
            result["resolution_mode"], "FACILITY_PRICELIST"
        )

    def test_no_contract_yields_no_fee_and_a_reason(self):
        result = applicable_fee(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertIsNone(result["amount"])
        self.assertEqual(
            result["reason_code"], ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT
        )

    def test_service_without_a_fee_row_yields_no_fee_match(self):
        self.active_with_fee()
        other = create_test_service(category="S")
        result = applicable_fee(
            self.health_facility, service=other, on_date=date(2026, 6, 1)
        )
        self.assertIsNone(result["amount"])
        self.assertEqual(
            result["reason_code"], ClaimScopeViolation.ReasonCode.NO_FEE_MATCH
        )

    def test_draft_schedule_prices_nothing(self):
        contract = self.build_contract(contract_number="PC-F2")
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        schedule = self.build_fee_schedule(contract=contract)  # stays DRAFT
        self.build_fee_item(fee_schedule=schedule, service=self.service)

        result = applicable_fee(
            self.health_facility, service=self.service, on_date=date(2026, 6, 1)
        )
        self.assertIsNone(result["amount"])

    def test_disabling_the_gate_makes_fee_lookup_inert(self):
        self.active_with_fee()
        config = get_config()
        with mock.patch.object(config, "pce_gate_enabled", False):
            result = applicable_fee(
                self.health_facility, service=self.service, on_date=date(2026, 6, 1)
            )
        self.assertIsNone(result["amount"])
        self.assertIsNone(result["reason_code"])


# policy.test_helpers.create_test_policy2 calls create_test_interactive_user()
# with the helper default password "admin123", which openIMIS's own complexity
# validator rejects. Nothing in this module chooses that password, so the
# validator is relaxed here rather than working around it in our fixtures.
@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class ViolationRecordingTests(ProviderContractTestCase):
    """Recording a finding must not touch the claim."""

    def setUp(self):
        super().setUp()
        self.svc = ClaimScopeViolationService(self.user)
        # A real claim row: ClaimScopeViolation carries a genuine FK to
        # claim.Claim, so a stub cannot be filtered against. The "claim is not
        # modified" guarantee is asserted by comparing the row's field values
        # before and after, not by watching attribute writes.
        self.claim = create_test_claim()
        self.claim_snapshot = _claim_state(self.claim)
        self.service = create_test_service(category="S")
        self.claim_service = create_test_claimservice(
            claim=self.claim, custom_props={"service": self.service}
        )
        self.verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )

    def assert_claim_untouched(self):
        self.assertEqual(
            _claim_state(self.claim),
            self.claim_snapshot,
            "the gate modified the claim; ADR-006 violated",
        )

    def test_a_negative_verdict_leaves_the_claim_untouched(self):
        self.svc.record(self.claim, self.health_facility, self.verdict, claim_service=self.claim_service)
        self.assert_claim_untouched()

    def test_a_positive_verdict_records_nothing(self):
        contract = self.build_contract(
            contract_number="PC-V1", date_valid_from=datetime(2025, 12, 1)
        )
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.assertIsNone(
            self.svc.record(self.claim, self.health_facility, verdict, claim_service=self.claim_service)
        )
        self.assertEqual(ClaimScopeViolation.objects.count(), 0)

    def test_recording_is_idempotent(self):
        first = self.svc.record(self.claim, self.health_facility, self.verdict, claim_service=self.claim_service)
        second = self.svc.record(self.claim, self.health_facility, self.verdict, claim_service=self.claim_service)
        self.assertEqual(first.id, second.id)
        self.assertEqual(ClaimScopeViolation.objects.count(), 1)

    def test_violation_captures_the_verdict(self):
        violation = self.svc.record(self.claim, self.health_facility, self.verdict, claim_service=self.claim_service)
        self.assertEqual(violation.reason_code, self.verdict["reason_code"])
        self.assertIn("reason", violation.details)

    def test_violation_is_attributed_to_the_actor(self):
        violation = self.svc.record(self.claim, self.health_facility, self.verdict, claim_service=self.claim_service)
        self.assertEqual(violation.user_created, self.user)


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class ViolationResolutionTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.svc = ClaimScopeViolationService(self.user)
        self.claim = create_test_claim()
        self.claim_service = create_test_claimservice(
            claim=self.claim, category="S"
        )
        verdict = check_provider_empanelment(
            self.health_facility, on_date=date(2026, 6, 1)
        )
        self.violation = self.svc.record(self.claim, self.health_facility, verdict, claim_service=self.claim_service)

    def test_resolve_marks_it_closed(self):
        resolved = self.svc.resolve(self.violation, "Contract retro-signed")
        self.assertTrue(resolved.is_resolved)
        self.assertEqual(resolved.reviewed_by, self.user)
        self.assertIsNotNone(resolved.date_resolved)

    def test_resolve_requires_a_note(self):
        with self.assertRaises(ValidationError):
            self.svc.resolve(self.violation, "")

    def test_waive_requires_a_reason(self):
        with self.assertRaises(ValidationError):
            self.svc.waive(self.violation, "")

    def test_waived_is_distinguishable_from_resolved(self):
        waived = self.svc.waive(self.violation, "Gate misfire")
        self.assertTrue(waived.is_resolved)
        self.assertIn("Waived", waived.resolution)

    def test_resolving_twice_is_refused(self):
        self.svc.resolve(self.violation, "done")
        self.violation.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.svc.resolve(self.violation, "again")

    def test_open_queue_orders_blocking_first(self):
        ClaimScopeViolation.objects.filter(pk=self.violation.pk).update(
            severity=ClaimScopeViolation.Severity.BLOCKING
        )
        warning = ClaimScopeViolation(
            claim_id=self.violation.claim_id,
            claim_service_id=self.violation.claim_service_id,
            provider=self.health_facility,
            # NOT NULL: the column records which service the finding was about.
            service_code=self.violation.service_code,
            reason_code=ClaimScopeViolation.ReasonCode.NO_FEE_MATCH,
            severity=ClaimScopeViolation.Severity.WARNING,
        )
        warning.save(user=self.user)

        queue = ClaimScopeViolationService.open_violations()
        self.assertEqual(
            [v.severity for v in queue],
            [
                ClaimScopeViolation.Severity.BLOCKING,
                ClaimScopeViolation.Severity.WARNING,
            ],
        )

    def test_resolved_violations_leave_the_queue(self):
        self.svc.resolve(self.violation, "fixed")
        self.assertEqual(ClaimScopeViolationService.open_violations(), [])
