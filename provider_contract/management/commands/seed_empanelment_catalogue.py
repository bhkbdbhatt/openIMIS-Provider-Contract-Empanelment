"""Seed the default empanelment workflow, its stages and the document checklist.

This is a management command rather than a data migration on purpose.

Every entity here inherits ``HistoryModel``, whose ``user_created`` and
``user_updated`` are non-nullable foreign keys to ``core.User``. A migration
would therefore have to invent an actor, and on a fresh install there is no
user to attribute the rows to yet -- the catalogue is precisely what a fresh
install needs first. openIMIS core reached the same conclusion: its own
``insert_role_right_for_system`` is stubbed out with the comment "do not
manage the role and right via migrations".

The command is idempotent, so it is safe to re-run after an upgrade and safe
to run on a populated database. Rows are matched on ``code``, so a deployment
that has customised a stage keeps its customisation.

Run it after the users exist::

    python manage.py seed_empanelment_catalogue --user admin
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import User

from provider_contract.models import (
    EmpanelmentDocumentType,
    EmpanelmentStage,
    EmpanelmentWorkflow,
)

DEFAULT_WORKFLOW = {
    "code": "DEFAULT",
    "name": "Standard provider empanelment",
    "description": (
        "Application, document verification, site visit, technical review and "
        "committee decision."
    ),
    "is_default": True,
    "sort_order": 1,
}

DEFAULT_STAGES = [
    {
        "code": "APPLICATION",
        "name": "Application received",
        "description": "Provider submitted a complete empanelment application.",
        "sequence": 1,
        "sla_days": 14,
        "is_decision_stage": False,
        "requires_all_documents": False,
    },
    {
        "code": "DOC_VERIFICATION",
        "name": "Document verification",
        "description": "Licence and accreditation documents checked for validity.",
        "sequence": 2,
        "sla_days": 21,
        "is_decision_stage": False,
        "requires_all_documents": True,
    },
    {
        "code": "SITE_VISIT",
        "name": "Site visit",
        "description": "Physical inspection of the facility.",
        "sequence": 3,
        "sla_days": 30,
        "is_decision_stage": False,
        "requires_all_documents": False,
    },
    {
        "code": "TECH_REVIEW",
        "name": "Technical review",
        "description": "Findings of the site visit assessed by the technical team.",
        "sequence": 4,
        "sla_days": 14,
        "is_decision_stage": False,
        "requires_all_documents": False,
    },
    {
        "code": "COMMITTEE",
        "name": "Committee decision",
        "description": "Empanelment committee records the final decision.",
        "sequence": 5,
        "sla_days": 30,
        "is_decision_stage": True,
        "requires_all_documents": False,
    },
]

DEFAULT_DOCUMENT_TYPES = [
    {
        "code": "LICENCE",
        "name": "Operating licence",
        "description": "Current operating licence issued by the authority.",
        "is_mandatory": True,
        "requires_expiry": True,
        "validity_months": 12,
        "sort_order": 1,
    },
    {
        "code": "ACCREDITATION",
        "name": "Accreditation certificate",
        "description": "Accreditation or certification of the facility.",
        "is_mandatory": True,
        "requires_expiry": True,
        "validity_months": 24,
        "sort_order": 2,
    },
    {
        "code": "TAX_CERT",
        "name": "Tax registration certificate",
        "description": "Proof of tax registration.",
        "is_mandatory": True,
        "requires_expiry": False,
        "validity_months": None,
        "sort_order": 3,
    },
    {
        "code": "BANK_LETTER",
        "name": "Bank confirmation letter",
        "description": "Bank letter confirming the facility account.",
        "is_mandatory": False,
        "requires_expiry": False,
        "validity_months": None,
        "sort_order": 4,
    },
    {
        "code": "STAFF_ROSTER",
        "name": "Staff roster",
        "description": "Roster of qualified staff at the facility.",
        "is_mandatory": True,
        "requires_expiry": False,
        "validity_months": None,
        "sort_order": 5,
    },
]


def _get_or_create_as_user(model, user, lookup, defaults):
    """``get_or_create`` cannot pass ``user`` through to ``save()``.

    ``HistoryModel.save()`` resolves the audit actor through ``get_user()``,
    which -- given neither ``user`` nor ``username`` -- falls back to
    ``User.objects.filter(i_user_id=1)``. Using ``get_or_create`` here would
    therefore either attribute the whole catalogue to an arbitrary user or, on
    an install with no such user, violate the NOT NULL constraint on
    ``user_created``. Doing the lookup by hand keeps the attribution explicit
    while preserving the idempotent match-on-``code`` behaviour.
    """
    obj = model.objects.filter(**lookup).first()
    if obj is not None:
        return obj, False
    obj = model(**{**lookup, **defaults})
    obj.save(user=user)
    return obj, True


class Command(BaseCommand):
    help = "Create the default empanelment workflow, stages and document checklist (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            dest="user",
            default=None,
            help="Username of the user to attribute the rows to. "
            "core.User has no uuid column, so this is the username. "
            "Defaults to the first superuser.",
        )

    def _resolve_user(self, username):
        if username:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f"No user with username {username!r}.")
            return user

        user = User.objects.filter(is_superuser=True).order_by("username").first()
        if user is None:
            user = User.objects.order_by("username").first()
        if user is None:
            raise CommandError(
                "No core.User exists yet. Create the first user (loaddata the openIMIS "
                "users fixture) and re-run, or pass --user explicitly."
            )
        return user

    @transaction.atomic
    def handle(self, *args, **options):
        user = self._resolve_user(options.get("user"))
        self.stdout.write(f"Seeding empanelment catalogue as user {user.username}")

        workflow, created = _get_or_create_as_user(
            EmpanelmentWorkflow,
            user,
            {"code": DEFAULT_WORKFLOW["code"]},
            {k: v for k, v in DEFAULT_WORKFLOW.items() if k != "code"},
        )
        self.stdout.write(
            f"  workflow {workflow.code}: {'created' if created else 'already present'}"
        )

        for stage in DEFAULT_STAGES:
            _, created = _get_or_create_as_user(
                EmpanelmentStage,
                user,
                {"workflow": workflow, "code": stage["code"]},
                {k: v for k, v in stage.items() if k != "code"},
            )
            self.stdout.write(
                f"  stage {stage['code']}: {'created' if created else 'already present'}"
            )

        for document_type in DEFAULT_DOCUMENT_TYPES:
            _, created = _get_or_create_as_user(
                EmpanelmentDocumentType,
                user,
                {"code": document_type["code"]},
                {k: v for k, v in document_type.items() if k != "code"},
            )
            self.stdout.write(
                f"  document {document_type['code']}: "
                f"{'created' if created else 'already present'}"
            )

        self.stdout.write(self.style.SUCCESS("Empanelment catalogue seeded."))
