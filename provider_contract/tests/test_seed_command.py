"""The default empanelment catalogue must be seedable and safe to re-run.

The command replaces what a data migration would otherwise do, and the whole
reason it is a command is that ``HistoryModel`` refuses to let a row be created
without an actor. That makes attribution the thing most worth testing here: a
catalogue seeded through ``get_or_create`` would silently pick up whatever
``get_user()`` happens to find, which is exactly the bug this module's
documentation warns about.
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from core.test_helpers import create_test_technical_user

from provider_contract.models import (
    EmpanelmentDocumentType,
    EmpanelmentStage,
    EmpanelmentWorkflow,
)

from .base import ProviderContractTestCase


class SeedCatalogueTests(ProviderContractTestCase):
    def seed(self, **kwargs):
        out = StringIO()
        call_command("seed_empanelment_catalogue", stdout=out, **kwargs)
        return out.getvalue()

    def test_creates_workflow_stages_and_document_types(self):
        self.seed(user="pce_test_admin")

        self.assertEqual(EmpanelmentWorkflow.objects.count(), 1)
        self.assertEqual(EmpanelmentStage.objects.count(), 5)
        self.assertEqual(EmpanelmentDocumentType.objects.count(), 5)

    def test_default_workflow_is_flagged_as_default(self):
        self.seed(user="pce_test_admin")
        self.assertTrue(EmpanelmentWorkflow.objects.get(code="DEFAULT").is_default)

    def test_stages_are_ordered_and_end_in_a_decision_stage(self):
        self.seed(user="pce_test_admin")

        stages = EmpanelmentStage.objects.order_by("sequence")
        self.assertEqual([s.sequence for s in stages], [1, 2, 3, 4, 5])
        self.assertEqual(stages.last().code, "COMMITTEE")
        self.assertTrue(stages.last().is_decision_stage)

    def test_rows_are_attributed_to_the_requested_user(self):
        """Regression guard: get_or_create cannot forward user= to save()."""
        actor = create_test_technical_user(username="pce_seed_actor")

        self.seed(user="pce_seed_actor")

        workflow = EmpanelmentWorkflow.objects.get(code="DEFAULT")
        self.assertEqual(workflow.user_created, actor)
        self.assertEqual(workflow.user_updated, actor)

    def test_every_seeded_row_carries_an_actor(self):
        self.seed(user="pce_test_admin")

        for model in (EmpanelmentWorkflow, EmpanelmentStage, EmpanelmentDocumentType):
            with self.subTest(model=model.__name__):
                unattributed = model.objects.filter(user_created__isnull=True)
                self.assertEqual(unattributed.count(), 0)

    def test_re_running_creates_nothing_new(self):
        self.seed(user="pce_test_admin")
        workflow_id = EmpanelmentWorkflow.objects.get(code="DEFAULT").id

        output = self.seed(user="pce_test_admin")

        self.assertEqual(EmpanelmentWorkflow.objects.count(), 1)
        self.assertEqual(EmpanelmentStage.objects.count(), 5)
        self.assertEqual(EmpanelmentDocumentType.objects.count(), 5)
        self.assertEqual(EmpanelmentWorkflow.objects.get(code="DEFAULT").id, workflow_id)
        self.assertIn("already present", output)

    def test_existing_stage_customisation_survives_a_re_run(self):
        self.seed(user="pce_test_admin")
        stage = EmpanelmentStage.objects.get(code="SITE_VISIT")
        stage.name = "Locally retitled site visit"
        stage.save(user=self.user)

        self.seed(user="pce_test_admin")

        stage.refresh_from_db()
        self.assertEqual(stage.name, "Locally retitled site visit")

    def test_unknown_username_is_rejected(self):
        with self.assertRaises(CommandError):
            self.seed(user="nobody_with_this_name")

    def test_falls_back_to_a_superuser_when_no_user_is_given(self):
        self.seed()
        self.assertTrue(EmpanelmentWorkflow.objects.filter(code="DEFAULT").exists())

    def test_mandatory_documents_are_marked_such(self):
        self.seed(user="pce_test_admin")

        mandatory = set(
            EmpanelmentDocumentType.objects.filter(is_mandatory=True).values_list(
                "code", flat=True
            )
        )
        self.assertEqual(mandatory, {"LICENCE", "ACCREDITATION", "TAX_CERT", "STAFF_ROSTER"})
        self.assertFalse(
            EmpanelmentDocumentType.objects.get(code="BANK_LETTER").is_mandatory
        )
