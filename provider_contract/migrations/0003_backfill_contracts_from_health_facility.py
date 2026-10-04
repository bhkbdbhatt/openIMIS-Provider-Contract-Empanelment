import datetime
import logging
import uuid

from django.db import migrations
from django.utils import timezone

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500

BACKFILL_NOTE = "Imported from legacy HealthFacility contract dates (ADR-003)."

# Spelled out rather than taken from ProviderContract.Status: the historical
# model a migration receives is a plain reconstruction from the migration
# state, so it carries the `status` field but not the nested TextChoices
# class. Reading ProviderContract.Status here raises AttributeError during a
# real `migrate`, which is exactly the trap in AGENTS section 8.
STATUS_ACTIVE = "ACTIVE"
STATUS_EXPIRED = "EXPIRED"


def _migration_actor(User):
    """Return a core.User to attribute the backfilled rows to.

    ``HistoryModel.user_created`` is a non-nullable foreign key to
    ``core.User``, and ``HealthFacility`` is a ``VersionedModel`` that carries
    no UUID audit user to copy across (only the legacy integer
    ``audit_user_id``). A migration therefore has to name an actor itself.

    The first user by username is used so the attribution is deterministic
    across re-runs and deployments. Following the precedent of core's own
    disabled ``insert_role_right_for_system``, a missing user is a warning and
    a skip rather than a failure: on a fresh install there are no facilities to
    backfill in the first place.

    Only ``username`` and ``id`` are ordered on because ``core.User`` is a
    ``TechnicalUser``/``AbstractBaseUser``: it has no ``date_created``, no
    ``validity_from`` and no ``uuid`` column.
    """
    actor = User.objects.order_by("username", "id").first()
    if actor is None:
        logger.warning(
            "Backfill of ProviderContract from HealthFacility skipped: "
            "no core.User exists to attribute the imported rows to."
        )
    return actor


def backfill_contracts(apps, schema_editor):
    """Create a ProviderContract for every facility carrying legacy contract dates.

    Forward-only: the imported contracts become financially significant as soon
    as claims are priced against them, so there is deliberately no reverse.
    Re-running is a no-op because a facility that already has a contract is
    skipped.
    """
    HealthFacility = apps.get_model("location", "HealthFacility")
    ProviderContract = apps.get_model("provider_contract", "ProviderContract")
    User = apps.get_model("core", "User")

    actor = _migration_actor(User)
    if actor is None:
        return

    today = datetime.date.today()
    created = 0
    skipped_existing = 0
    skipped_unusable = 0

    facilities = (
        HealthFacility.objects.exclude(contract_start_date__isnull=True)
        .only("id", "contract_start_date", "contract_end_date")
        .order_by("id")
        .iterator(chunk_size=CHUNK_SIZE)
    )

    for facility in facilities:
        if ProviderContract.objects.filter(provider_id=facility.id).exists():
            skipped_existing += 1
            continue

        start = facility.contract_start_date
        end = facility.contract_end_date or start

        # pce_contract_dates_ordered requires date_end >= date_start. Legacy
        # data is not guaranteed to satisfy that, and a contract that ends
        # before it starts carries no meaning worth importing.
        if end < start:
            skipped_unusable += 1
            continue

        ProviderContract.objects.create(
            # apps.get_model() yields a *plain* historical model: none of the
            # HistoryModel conveniences are present, so the UUID primary key
            # (which the live model assigns in save() via set_pk()) has to be
            # supplied here or the insert violates the NOT NULL constraint.
            id=uuid.uuid4(),
            provider_id=facility.id,
            # Derived from the facility primary key so the import is
            # deterministic and idempotent. max_length is 30; "LEGACY-" plus
            # an AutoField always fits.
            contract_number=f"LEGACY-{facility.id}",
            status=(STATUS_EXPIRED if end < today else STATUS_ACTIVE),
            version_no=1,
            date_start=start,
            date_end=end,
            # date_valid_to is exclusive (filter_validity uses date_valid_to >
            # date), so step one day past the inclusive date_end to keep the
            # contract valid for the whole of its last day. The boundaries are
            # made aware explicitly: openIMIS runs with USE_TZ and a naive
            # midnight would be ambiguous.
            date_valid_from=timezone.make_aware(
                datetime.datetime.combine(start, datetime.time.min)
            ),
            date_valid_to=timezone.make_aware(
                datetime.datetime.combine(
                    end + datetime.timedelta(days=1), datetime.time.min
                )
            ),
            notes=BACKFILL_NOTE,
            user_created=actor,
            user_updated=actor,
        )
        created += 1

    logger.info(
        "Backfilled %s ProviderContract row(s) from HealthFacility "
        "(%s already contracted, %s skipped as unusable legacy dates).",
        created,
        skipped_existing,
        skipped_unusable,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0037_historicaluser_is_superuser_user_is_superuser_and_more'),
        ('location', '0019_alter_location_code'),
        ('provider_contract', '0002_claim_scope_violation'),
    ]

    operations = [
        migrations.RunPython(
            code=backfill_contracts,
            # Forward-only on purpose: these contracts become financially
            # significant once claims are priced against them, and a reverse
            # would delete financial history rather than restore it.
            reverse_code=migrations.RunPython.noop,
        ),
    ]
