"""Behaviour of the forward-only backfill in migration 0003.

The migration module is exercised directly rather than through
``MigrationExecutor``: driving 0003 through the executor means migrating
backwards and forwards across the whole graph mid-test, and the backfill
function only ever touches ``apps.get_model``.

The registry handed to it is deliberately *not* the live one. A real migration
receives a state registry whose models are plain reconstructions -- no
``set_pk()``, no version bumping, and crucially no ``HistoryModel.save()``. The
live registry would silently paper over exactly the defects this module is
supposed to survive (see AGENTS section 8), so ``ProjectState.from_apps`` is
used to get models with the same shape and none of that behaviour.

What is actually being protected here is money. Once claims have been priced
against a backfilled contract the row cannot be undone, so the cases below are
about refusing to import bad legacy data rather than about a happy path.
"""

import datetime
import importlib
import uuid

from django.apps import apps as django_apps
from django.db.migrations.state import ProjectState

from location.test_helpers import create_test_health_facility

from provider_contract.models import ProviderContract

from .base import ProviderContractTestCase

migration = importlib.import_module(
    "provider_contract.migrations.0003_backfill_contracts_from_health_facility"
)


class BackfillTests(ProviderContractTestCase):
    def run_backfill(self):
        return migration.backfill_contracts(self.historical_apps(), None)

    @staticmethod
    def historical_apps():
        """Models as a migration sees them: same fields, none of the behaviour."""
        return ProjectState.from_apps(django_apps).apps

    def make_facility(self, code, start=None, end=None):
        return create_test_health_facility(
            code=code,
            custom_props={
                "contract_start_date": datetime.date.fromisoformat(start)
                if start
                else None,
                "contract_end_date": datetime.date.fromisoformat(end) if end else None,
            },
        )

    def test_imports_a_facility_carrying_legacy_dates(self):
        facility = self.make_facility("PCE-BF-1", "2026-01-01", "2026-12-31")

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertEqual(contract.contract_number, f"LEGACY-{facility.id}")
        self.assertEqual(contract.version_no, 1)
        self.assertEqual(contract.date_start, datetime.date(2026, 1, 1))
        self.assertEqual(contract.date_end, datetime.date(2026, 12, 31))
        self.assertEqual(contract.status, ProviderContract.Status.ACTIVE)
        self.assertIn("ADR-003", contract.notes)

    def test_imported_row_gets_an_explicit_uuid_pk(self):
        """apps.get_model() is a plain model: nothing assigns the UUID for it."""
        facility = self.make_facility("PCE-BF-2", "2026-01-01", "2026-12-31")

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertIsInstance(contract.pk, uuid.UUID)
        self.assertEqual(contract.uuid, contract.pk)

    def test_imported_row_is_attributed_to_a_real_user(self):
        """user_created is non-nullable, so an unattributed row cannot be inserted."""
        facility = self.make_facility("PCE-BF-3", "2026-01-01", "2026-12-31")

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertIsNotNone(contract.user_created_id)
        self.assertIsNotNone(contract.user_updated_id)

    def test_null_end_date_falls_back_to_the_start_date(self):
        facility = self.make_facility("PCE-BF-4", "2026-03-01", None)

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertEqual(contract.date_end, datetime.date(2026, 3, 1))

    def test_validity_covers_the_whole_of_the_last_day(self):
        """date_valid_to is exclusive, so it must land one day past date_end."""
        facility = self.make_facility("PCE-BF-5", "2026-03-01", "2026-03-31")

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertEqual(
            contract.date_valid_to.date(), datetime.date(2026, 4, 1)
        )

    def test_elapsed_contract_is_imported_as_expired(self):
        facility = self.make_facility("PCE-BF-6", "2019-01-01", "2019-12-31")

        self.run_backfill()

        contract = ProviderContract.objects.get(provider=facility)
        self.assertEqual(contract.status, ProviderContract.Status.EXPIRED)

    def test_facility_without_a_start_date_is_skipped(self):
        self.make_facility("PCE-BF-7", None, "2026-12-31")

        self.run_backfill()

        self.assertEqual(ProviderContract.objects.count(), 0)

    def test_inverted_legacy_dates_are_skipped(self):
        """pce_contract_dates_ordered would reject the row; refuse to import it."""
        facility = self.make_facility("PCE-BF-8", "2026-12-31", "2026-01-01")

        self.run_backfill()

        self.assertFalse(
            ProviderContract.objects.filter(provider=facility).exists()
        )

    def test_re_running_is_a_no_op(self):
        facility = self.make_facility("PCE-BF-9", "2026-01-01", "2026-12-31")

        self.run_backfill()
        self.run_backfill()

        self.assertEqual(
            ProviderContract.objects.filter(provider=facility).count(), 1
        )

    def test_facility_that_already_has_a_contract_is_left_alone(self):
        facility = self.make_facility("PCE-BF-X", "2026-01-01", "2026-12-31")
        existing = self.build_contract(provider=facility, contract_number="PC-EXISTING")

        self.run_backfill()

        self.assertEqual(
            ProviderContract.objects.filter(provider=facility).count(), 1
        )
        self.assertEqual(
            ProviderContract.objects.get(provider=facility).contract_number,
            existing.contract_number,
        )


class _EmptyUserModel:
    """Stand-in for core.User on a fresh install with no users yet."""

    class objects:
        @staticmethod
        def order_by(*fields):
            return _EmptyUserModel.objects

        @staticmethod
        def first():
            return None


class BackfillActorTests(ProviderContractTestCase):
    def test_actor_is_none_when_no_user_exists(self):
        """A fresh install has no core.User; skip instead of crashing."""
        self.assertIsNone(migration._migration_actor(_EmptyUserModel))

    def test_actor_is_the_lowest_username(self):
        from core.models import User
        from core.test_helpers import create_test_technical_user

        create_test_technical_user(username="aaa_backfill_actor")
        create_test_technical_user(username="zzz_backfill_actor")

        actor = migration._migration_actor(User)

        self.assertEqual(actor.username, "aaa_backfill_actor")
