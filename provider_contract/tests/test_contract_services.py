"""Contract lifecycle service tests.

The state machine and the two uniqueness rules are the load-bearing parts of
this module. The uniqueness rules in particular are enforced in Python rather
than by the database (ADR-002), which means they are only as good as these
tests: nothing in the schema would catch a regression.
"""

from datetime import date, datetime, timedelta
from unittest import mock

from django.core.exceptions import ValidationError

from location.test_helpers import create_test_health_facility

from provider_contract.models import ContractFeeSchedule, ProviderContract
from provider_contract.services import ProviderContractService
from provider_contract.services.common import get_config

from .base import ProviderContractTestCase


class ContractCreationTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.svc = ProviderContractService(self.user)

    def test_create_starts_as_draft_at_version_one(self):
        contract = self.svc.create(
            {
                "provider_id": self.health_facility.uuid,
                "contract_number": "PC-1",
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )
        self.assertEqual(contract.status, ProviderContract.Status.DRAFT)
        self.assertEqual(contract.version_no, 1)

    def test_create_attaches_the_actor(self):
        contract = self.svc.create(
            {
                "provider_id": self.health_facility.uuid,
                "contract_number": "PC-2",
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )
        self.assertEqual(contract.user_created, self.user)

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.create(
                {
                    "provider_id": "00000000-0000-0000-0000-00000000dead",
                    "contract_number": "PC-3",
                    "date_start": "2026-01-01",
                    "date_end": "2026-12-31",
                }
            )

    def test_inverted_dates_are_rejected_before_the_database_sees_them(self):
        with self.assertRaises(ValidationError):
            self.svc.create(
                {
                    "provider_id": self.health_facility.uuid,
                    "contract_number": "PC-4",
                    "date_start": "2026-12-31",
                    "date_end": "2026-01-01",
                }
            )

    def test_second_open_version_of_a_contract_number_is_refused(self):
        """ADR-002: uniqueness enforced in services, not by a partial index."""
        self.svc.create(
            {
                "provider_id": self.health_facility.uuid,
                "contract_number": "PC-5",
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )
        with self.assertRaises(ValidationError):
            self.svc.create(
                {
                    "provider_id": self.health_facility.uuid,
                    "contract_number": "PC-5",
                    "date_start": "2027-01-01",
                    "date_end": "2027-12-31",
                }
            )
        self.assertEqual(
            ProviderContract.objects.filter(contract_number="PC-5").count(), 1
        )


class ContractTransitionTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.svc = ProviderContractService(self.user)
        self.contract = self.build_contract(contract_number="PC-T1")

    def signed_contract(self, **kwargs):
        contract = self.build_contract(**kwargs)
        # Draft -> Submitted -> Under negotiation. approve() only accepts
        # NEGOTIATION, so the submit step is not optional here.
        self.svc.submit(contract)
        self.svc.approve(contract)
        return contract

    def activated_contract(self, **kwargs):
        contract = self.signed_contract(**kwargs)
        schedule = self.build_fee_schedule(contract=contract)
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)
        self.svc.activate(contract)
        return contract

    def test_submit_moves_draft_to_negotiation(self):
        self.assertEqual(self.svc.submit(self.contract).status,
                         ProviderContract.Status.NEGOTIATION)

    def test_submit_twice_is_refused(self):
        self.svc.submit(self.contract)
        with self.assertRaises(ValidationError):
            self.svc.submit(self.contract)

    def test_approve_stamps_the_signature_date(self):
        self.svc.submit(self.contract)
        approved = self.svc.approve(self.contract, date_signed="2026-01-05")
        self.assertEqual(approved.status, ProviderContract.Status.SIGNED)
        self.assertEqual(approved.date_signed, date(2026, 1, 5))

    def test_cannot_approve_a_draft(self):
        with self.assertRaises(ValidationError):
            self.svc.approve(self.contract)

    def test_activate_requires_an_active_fee_schedule(self):
        """A contract with no prices would fall back to the default pricelist."""
        self.svc.submit(self.contract)
        self.svc.approve(self.contract)
        with self.assertRaises(ValidationError):
            self.svc.activate(self.contract)

    def test_activate_moves_signed_to_active(self):
        contract = self.activated_contract(contract_number="PC-T2")
        self.assertEqual(contract.status, ProviderContract.Status.ACTIVE)

    def test_only_one_active_contract_per_facility_in_pricelist_mode(self):
        self.activated_contract(contract_number="PC-T3")
        second = self.signed_contract(contract_number="PC-T4")
        schedule = self.build_fee_schedule(contract=second)
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)

        with self.assertRaises(ValidationError):
            self.svc.activate(second)

    def test_concurrent_contracts_are_allowed_in_contract_calcrule_mode(self):
        self.activated_contract(contract_number="PC-T5")
        second = self.signed_contract(contract_number="PC-T6")
        schedule = self.build_fee_schedule(contract=second)
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)

        # The mode lives on the AppConfig as a class attribute, not in
        # django.conf.settings, so override_settings() cannot reach it.
        config = get_config()
        with mock.patch.object(config, "pce_fee_resolution_mode", "CONTRACT_CALCULE"):
            self.assertEqual(
                self.svc.activate(second).status, ProviderContract.Status.ACTIVE
            )

    def test_suspend_and_resume(self):
        contract = self.activated_contract(contract_number="PC-T7")
        self.assertEqual(
            self.svc.suspend(contract, "Fraud under investigation").status,
            ProviderContract.Status.SUSPENDED,
        )
        self.assertEqual(self.svc.resume(contract).status,
                         ProviderContract.Status.ACTIVE)

    def test_terminate_requires_a_reason(self):
        contract = self.activated_contract(contract_number="PC-T8")
        with self.assertRaises(ValidationError):
            self.svc.terminate(contract, reason="")

    def test_terminate_closes_the_version(self):
        contract = self.activated_contract(contract_number="PC-T9")
        terminated = self.svc.terminate(contract, "Provider relocated")
        self.assertEqual(terminated.status, ProviderContract.Status.TERMINATED)
        self.assertIsNotNone(terminated.date_valid_to)

    def test_delete_is_refused_once_agreed(self):
        contract = self.activated_contract(contract_number="PC-T10")
        with self.assertRaises(ValidationError):
            self.svc.delete(contract)

    def test_draft_can_be_deleted(self):
        self.svc.delete(self.contract)
        self.assertTrue(
            ProviderContract.objects.filter(id=self.contract.id).exists()
        )
        self.assertNotIn(
            self.contract,
            ProviderContract.filter_queryset(ProviderContract.objects.all()),
        )


class ContractAmendmentTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.svc = ProviderContractService(self.user)

    def activated(self, number="PC-A1", **kwargs):
        contract = self.build_contract(contract_number=number, **kwargs)
        self.svc.submit(contract)
        self.svc.approve(contract)
        schedule = self.build_fee_schedule(contract=contract)
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)
        self.svc.activate(contract)
        return contract

    def test_amend_opens_a_new_version_rather_than_editing(self):
        contract = self.activated()

        amended = self.svc.amend(contract, {"date_end": "2027-06-30"})

        self.assertNotEqual(amended.id, contract.id)
        self.assertEqual(amended.version_no, 2)
        self.assertEqual(amended.date_end, date(2027, 6, 30))
        # The superseded version is closed, not deleted.
        contract.refresh_from_db()
        self.assertIsNotNone(contract.date_valid_to)
        self.assertTrue(ProviderContract.objects.filter(id=contract.id).exists())

    def test_amended_version_links_back_to_its_predecessor(self):
        contract = self.activated()
        amended = self.svc.amend(contract, {"date_end": "2027-06-30"})
        # The chain is a forward link stored on the superseded row:
        # replace_object writes the successor's id onto the row it replaced.
        contract.refresh_from_db()
        self.assertEqual(contract.replacement_uuid, amended.id)
        self.assertIsNone(amended.replacement_uuid)

    def test_amendment_resets_status_to_draft(self):
        contract = self.activated()
        amended = self.svc.amend(contract, {"notes": "new terms"})
        self.assertEqual(amended.status, ProviderContract.Status.DRAFT)

    def test_historical_pricing_is_unaffected_by_an_amendment(self):
        """The point of versioning: what was priced stays reproducible."""
        from provider_contract.services import contracts_covering

        contract = self.activated(date_start="2026-01-01", date_end="2026-06-30")
        # Backdate so the version really was in force in March; otherwise the
        # assertion compares 0 against 0 and proves nothing.
        contract.date_valid_from = datetime(2025, 12, 1)
        contract.save(user=self.user)

        before = contracts_covering(self.health_facility, date(2026, 3, 1))
        self.assertIn(contract, before, "fixture must cover the pricing date")

        self.svc.amend(contract, {"date_end": "2027-06-30"})

        after = contracts_covering(self.health_facility, date(2026, 3, 1))
        self.assertEqual(list(before), list(after))

    def test_terminated_contract_cannot_be_amended(self):
        contract = self.activated()
        self.svc.terminate(contract, "done")
        contract.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.svc.amend(contract, {"date_end": "2027-06-30"})


class ContractRenewalTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        self.svc = ProviderContractService(self.user)
        self.contract = self.build_contract(
            contract_number="PC-R1",
            date_start="2026-01-01",
            date_end="2026-12-31",
        )
        self.svc.submit(self.contract)
        self.svc.approve(self.contract)
        schedule = self.build_fee_schedule(contract=self.contract)
        schedule.status = ContractFeeSchedule.Status.ACTIVE
        schedule.save(user=self.user)
        self.svc.activate(self.contract)

    def test_renew_starts_the_new_term_the_day_after_the_old_one_ends(self):
        renewed = self.svc.renew(self.contract, date_end="2027-12-31")
        self.assertEqual(renewed.date_start, date(2027, 1, 1))
        self.assertEqual(renewed.date_end, date(2027, 12, 31))

    def test_renew_marks_the_old_version_renewed(self):
        self.svc.renew(self.contract, date_end="2027-12-31")
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, ProviderContract.Status.RENEWED)

    def test_renew_increments_the_version(self):
        renewed = self.svc.renew(self.contract, date_end="2027-12-31")
        self.assertEqual(renewed.version_no, 2)

    def test_renew_requires_an_end_date(self):
        with self.assertRaises(ValidationError):
            self.svc.renew(self.contract)

    def test_renew_rejects_inverted_dates(self):
        with self.assertRaises(ValidationError):
            self.svc.renew(
                self.contract, date_start="2027-06-30", date_end="2027-01-01"
            )

    def test_renewed_term_does_not_overlap_the_old_one(self):
        renewed = self.svc.renew(self.contract, date_end="2027-12-31")
        self.assertGreater(renewed.date_start, self.contract.date_end)


class ContractCoverageTests(ProviderContractTestCase):
    """Dated lookups: inclusive ``date_end``, exclusive ``date_valid_to``."""

    def setUp(self):
        super().setUp()
        self.svc = ProviderContractService(self.user)
        # Backdated: the version has to have been *in force* during 2026 for
        # contracts_covering(..., date(2026, ...)) to mean anything.
        # ValidityMixin.date_valid_from defaults to now(), and a version
        # recorded today correctly refuses to price a service from June.
        self.contract = self.build_contract(
            contract_number="PC-C1",
            date_start="2026-01-01",
            date_end="2026-12-31",
            date_valid_from=datetime(2025, 12, 1),
        )

    def test_last_day_of_the_term_is_covered(self):
        from provider_contract.services import contracts_covering

        self.assertIn(
            self.contract, contracts_covering(self.health_facility, date(2026, 12, 31))
        )

    def test_day_after_the_term_is_not_covered(self):
        from provider_contract.services import contracts_covering

        self.assertNotIn(
            self.contract, contracts_covering(self.health_facility, date(2027, 1, 1))
        )

    def test_day_before_the_term_is_not_covered(self):
        from provider_contract.services import contracts_covering

        self.assertNotIn(
            self.contract, contracts_covering(self.health_facility, date(2025, 12, 31))
        )

    def test_active_lookup_ignores_a_non_active_contract(self):
        from provider_contract.services import active_contract_for

        self.assertIsNone(active_contract_for(self.health_facility, date(2026, 6, 1)))
        self.contract.status = ProviderContract.Status.ACTIVE
        self.contract.save(user=self.user)
        self.assertEqual(
            active_contract_for(self.health_facility, date(2026, 6, 1)).id,
            self.contract.id,
        )

    def test_coverage_is_scoped_to_the_provider(self):
        from provider_contract.services import contracts_covering

        other = create_test_health_facility(code="PCE-HF-2")
        self.assertNotIn(self.contract, contracts_covering(other, date(2026, 6, 1)))

    def test_expiry_sweep_is_idempotent(self):
        from provider_contract.services import expire_contracts

        self.contract.status = ProviderContract.Status.ACTIVE
        # date_start has to move too: pce_contract_dates_ordered forbids an end
        # that precedes the start.
        self.contract.date_start = date(2020, 1, 1)
        self.contract.date_end = date(2020, 12, 31)
        self.contract.save(user=self.user)

        self.assertEqual(expire_contracts(self.user, reference=date(2026, 1, 1)), 1)
        self.assertEqual(expire_contracts(self.user, reference=date(2026, 1, 1)), 0)
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.status, ProviderContract.Status.EXPIRED)

    def test_expiring_window_is_configurable(self):
        from provider_contract.services import contracts_expiring_within

        self.contract.status = ProviderContract.Status.ACTIVE
        self.contract.date_end = date.today() + timedelta(days=30)
        self.contract.save(user=self.user)
        self.assertIn(self.contract, contracts_expiring_within(days=60))
        self.assertNotIn(self.contract, contracts_expiring_within(days=7))
