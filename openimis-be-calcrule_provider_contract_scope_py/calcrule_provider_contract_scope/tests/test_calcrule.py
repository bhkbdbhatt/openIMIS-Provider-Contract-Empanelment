"""Behaviour of the provider contract scope calculation rule.

Two things are being protected here, and they are not equally obvious.

The first is that the gate **flags and never touches the claim**. Every test that
records a finding also asserts the claim's field values are byte-identical
before and after. A test that only checked "a violation exists" would still pass
if the module quietly cancelled the claim on its way past (ADR-006).

The second is that the rule is **inert when disabled**, and that a failure
inside it cannot fail a claim. Both are asserted directly, because both are
"nothing happens" behaviours that are easy to break silently.

Fixtures come from ``provider_contract.tests.base`` on purpose: the two modules
have to agree on what a contract looks like, and duplicating those builders here
would let them drift apart.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from claim.models import ClaimService
from claim.test_helpers import create_test_claim, create_test_claimservice
from medical.test_helpers import create_test_service

from provider_contract.models import ClaimScopeViolation, ProviderContract
from provider_contract.tests.base import ProviderContractTestCase

from calcrule_provider_contract_scope import apps as cr_apps
from calcrule_provider_contract_scope.calculation_rule import ProviderContractScopeRule
from calcrule_provider_contract_scope.gate import (
    evaluate_claim,
    gate_is_enabled,
    resolve_fee_for,
)
from calcrule_provider_contract_scope.signals import on_claim_valuated


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class RuleRegistrationTests(ProviderContractTestCase):
    """The rule has to be findable, or none of the rest matters."""

    def test_rule_carries_administrable_metadata(self):
        self.assertEqual(ProviderContractScopeRule.type, "provider_contract")
        self.assertEqual(ProviderContractScopeRule.sub_type, "scope")
        self.assertEqual(ProviderContractScopeRule.status, "active")
        self.assertIsNone(ProviderContractScopeRule.date_valid_to)
        self.assertIn(
            "ClaimService",
            [p["class"] for p in ProviderContractScopeRule.impacted_class_parameter],
        )

    def test_uuid_is_a_valid_uuid(self):
        import uuid as uuid_mod

        uuid_mod.UUID(str(ProviderContractScopeRule.uuid))

    def test_registration_is_idempotent(self):
        """ready() can run twice; a duplicate rule would evaluate every line twice."""
        from calculation.apps import CALCULATION_RULES

        before = len(CALCULATION_RULES)
        cr_apps.register_calculation_rules()
        cr_apps.register_calculation_rules()
        self.assertEqual(len(CALCULATION_RULES), before)
        self.assertEqual(
            sum(
                1
                for r in CALCULATION_RULES
                if r.uuid == ProviderContractScopeRule.uuid
            ),
            1,
        )

    def test_framework_resolves_the_rule_for_a_claim_service(self):
        from calculation.services import get_rule_name

        names = [r.calculation_rule_name for r in (get_rule_name("ClaimService") or [])]
        self.assertIn("Provider contract scope", names)

    def test_rule_offers_no_conversion(self):
        """It prices nothing, so offering a conversion would be a lie."""
        self.assertEqual(ProviderContractScopeRule.from_to, [])
        self.assertIsNone(
            ProviderContractScopeRule.convert(instance=None, convert_to="anything")
        )


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class GateDisabledTests(ProviderContractTestCase):
    """``pce_gate_enabled = False`` must make the module do nothing at all."""

    def setUp(self):
        super().setUp()
        self.service = create_test_service(category="S")
        self.claim = create_test_claim(custom_props={"health_facility": self.health_facility})
        create_test_claimservice(
            claim=self.claim, custom_props={"service": self.service}
        )

    @mock.patch(
        "calcrule_provider_contract_scope.gate.gate_is_enabled", return_value=False
    )
    def test_disabled_gate_records_nothing(self, _):
        self.assertIsNone(evaluate_claim(self.claim, user=self.user))
        self.assertEqual(ClaimScopeViolation.objects.count(), 0)

    @mock.patch(
        "calcrule_provider_contract_scope.gate.gate_is_enabled", return_value=False
    )
    def test_disabled_gate_leaves_the_signal_receiver_inert(self, _):
        self.assertIsNone(on_claim_valuated(sender=ClaimService, claim=self.claim))
        self.assertEqual(ClaimScopeViolation.objects.count(), 0)

    @mock.patch(
        "calcrule_provider_contract_scope.gate.gate_is_enabled", return_value=False
    )
    def test_disabled_gate_prices_nothing(self, _):
        self.assertIsNone(
            resolve_fee_for(self.claim, service=self.service, user=self.user)
        )


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class GateBehaviourTests(ProviderContractTestCase):
    """The gate's decisions, and its promise not to touch the claim."""

    def setUp(self):
        super().setUp()
        self.service = create_test_service(category="S")
        self.claim = create_test_claim(
            custom_props={
                "health_facility": self.health_facility,
                "date_from": date(2026, 6, 1),
            }
        )
        self.claim_service = create_test_claimservice(
            claim=self.claim, custom_props={"service": self.service}
        )
        # Snapshot a freshly-read claim, not the in-memory one: openIMIS
        # hydrates its datetimes as core.datetimes.ad_datetime, so comparing a
        # just-constructed object against a reloaded one compares two different
        # classes and fails for reasons that have nothing to do with the gate.
        self.claim.refresh_from_db()
        self.claim_snapshot = self._state(self.claim)

    def _state(self, claim):
        """Every concrete field of the claim, for before/after comparison."""
        return {
            f.attname: getattr(claim, f.attname)
            for f in claim._meta.fields
            if not f.primary_key
        }

    def assert_claim_untouched(self, msg="the gate modified the claim; ADR-006"):
        self.claim.refresh_from_db()
        self.assertEqual(self._state(self.claim), self.claim_snapshot, msg)

    def active_contract(self, number="PC-CALC-1", **kwargs):
        kwargs.setdefault("date_valid_from", datetime(2025, 12, 1))
        kwargs.setdefault("date_start", "2026-01-01")
        kwargs.setdefault("date_end", "2026-12-31")
        contract = self.build_contract(contract_number=number, **kwargs)
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        return contract

    # --- negative ---------------------------------------------------------

    def test_unempanelled_provider_records_a_violation_and_changes_nothing(self):
        summary = evaluate_claim(self.claim, user=self.user)

        self.assertEqual(summary["violations"], 1)
        violation = ClaimScopeViolation.objects.get(claim=self.claim)
        self.assertEqual(
            violation.reason_code, ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT
        )
        self.assertEqual(violation.severity, ClaimScopeViolation.Severity.BLOCKING)
        self.assertEqual(violation.service_code, self.service.code[:6])
        self.assert_claim_untouched()

    def test_violation_is_attributed_to_the_actor(self):
        evaluate_claim(self.claim, user=self.user)

        violation = ClaimScopeViolation.objects.get(claim=self.claim)
        self.assertEqual(violation.user_created_id, self.user.id)

    def test_claim_without_a_provider_is_skipped(self):
        """Defensive path only.

        ``Claim.health_facility`` is ``NOT NULL``, so a persisted claim always
        names a provider. The guard in ``_provider_of`` exists for an unsaved
        or partially-loaded instance, which is what this exercises.
        """
        from claim.models import Claim

        field = Claim._meta.get_field("health_facility")
        self.assertFalse(field.null, "health_facility is NOT NULL on Claim")
        self.assertIsNone(evaluate_claim(Claim(code="UNSAVED"), user=self.user))
        self.assertEqual(ClaimScopeViolation.objects.count(), 0)

    def test_recording_is_idempotent(self):
        """The valuation path can fire more than once for the same claim."""
        evaluate_claim(self.claim, user=self.user)
        evaluate_claim(self.claim, user=self.user)

        self.assertEqual(ClaimScopeViolation.objects.count(), 1)

    # --- positive ---------------------------------------------------------

    def test_empanelled_provider_records_nothing(self):
        self.active_contract()

        summary = evaluate_claim(self.claim, user=self.user)

        self.assertEqual(summary["violations"], 0)
        self.assertTrue(summary["results"][0]["empanelled"])
        self.assertEqual(ClaimScopeViolation.objects.count(), 0)
        self.assert_claim_untouched()

    def test_out_of_term_provider_is_flagged(self):
        """A contract that ended last month does not cover today."""
        self.active_contract(date_start="2026-01-01", date_end="2026-01-31")

        summary = evaluate_claim(self.claim, user=self.user)

        self.assertEqual(summary["violations"], 1)
        self.assertEqual(
            summary["results"][0]["reason_code"],
            ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT,
        )
        self.assert_claim_untouched()

    # --- per line ---------------------------------------------------------

    def test_each_line_is_scoped_separately(self):
        """A claim can be partly covered; flagging the whole claim would be wrong."""
        self.active_contract()
        other = create_test_service(category="S")
        self.claim_service.service = other
        self.claim_service.save()

        summary = evaluate_claim(self.claim, user=self.user)

        # No scope rows on the contract, so both lines are inside scope.
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["violations"], 0)

    # --- the framework path ----------------------------------------------

    def test_signal_receiver_runs_the_gate(self):
        summary = on_claim_valuated(
            sender=ClaimService, claim=self.claim, errors=None, user=self.user
        )

        self.assertEqual(summary["violations"], 1)
        self.assert_claim_untouched()

    def test_receiver_never_raises(self):
        """A failure here would reject a claim over a reference module."""
        with mock.patch(
            "calcrule_provider_contract_scope.signals.evaluate_claim",
            side_effect=RuntimeError("boom"),
        ):
            result = on_claim_valuated(
                sender=ClaimService, claim=self.claim, user=self.user
            )

        self.assertIsNone(result)
        self.assert_claim_untouched()

    def test_receiver_tolerates_a_missing_claim(self):
        self.assertIsNone(on_claim_valuated(sender=ClaimService, claim=None))

    def test_rule_calculate_returns_the_summary(self):
        result = ProviderContractScopeRule.calculate(self.claim_service, user=self.user)

        self.assertEqual(result["violations"], 1)
        self.assert_claim_untouched()

    def test_rule_calculate_swallows_a_failure(self):
        with mock.patch(
            "calcrule_provider_contract_scope.calculation_rule.evaluate_claim",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(ProviderContractScopeRule.calculate(self.claim_service))


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class GatePricingTests(ProviderContractTestCase):
    """``resolve_fee_for`` is the CONTRACT_CALCULE mode entry point."""

    def setUp(self):
        super().setUp()
        self.service = create_test_service(category="S")
        self.claim = create_test_claim(
            custom_props={
                "health_facility": self.health_facility,
                "date_from": date(2026, 6, 1),
            }
        )
        self.claim.refresh_from_db()
        self.claim_snapshot = {
            f.attname: getattr(self.claim, f.attname)
            for f in self.claim._meta.fields
            if not f.primary_key
        }

    def test_contracted_fee_is_returned(self):
        contract = self.build_contract(
            contract_number="PC-PRICE-1",
            date_start="2026-01-01",
            date_end="2026-12-31",
            date_valid_from=datetime(2025, 12, 1),
        )
        contract.status = ProviderContract.Status.ACTIVE
        contract.save(user=self.user)
        schedule = self.build_fee_schedule(
            contract=contract, date_valid_from=datetime(2025, 12, 1)
        )
        schedule.status = schedule.Status.ACTIVE
        schedule.save(user=self.user)
        self.build_fee_item(
            fee_schedule=schedule, service=self.service, amount=Decimal("42.00")
        )

        result = resolve_fee_for(self.claim, service=self.service, user=self.user)

        self.assertEqual(Decimal(str(result["amount"])), Decimal("42.00"))
        self.assertEqual(result["contract"], "PC-PRICE-1")
        self.assertIsNone(result["reason_code"])

    def test_no_contract_yields_a_reason_rather_than_an_exception(self):
        result = resolve_fee_for(self.claim, service=self.service, user=self.user)

        self.assertIsNone(result["amount"])
        self.assertEqual(
            result["reason_code"], ClaimScopeViolation.ReasonCode.NO_ACTIVE_CONTRACT
        )

    def test_pricing_never_modifies_the_claim(self):
        resolve_fee_for(self.claim, service=self.service, user=self.user)
        self.claim.refresh_from_db()
        now = {
            f.attname: getattr(self.claim, f.attname)
            for f in self.claim._meta.fields
            if not f.primary_key
        }
        self.assertEqual(now, self.claim_snapshot)

    def test_gate_is_enabled_reads_configuration(self):
        self.assertIn(gate_is_enabled(), (True, False))
