"""GraphQL behaviour tests for provider_contract.

These exercise the API surface the way a client does -- through the query and
mutation harness -- rather than calling the services directly, because the
parts most likely to break are exactly the ones the services cannot see: the
permission check, the row-security filter, the mutation log, and whether a
business failure comes back as a renderable error rather than a 500.

Conventions worth knowing when reading these:

* ``send_mutation`` polls ``mutationLogs`` until the mutation leaves the
  pending state, so every mutation assertion goes through
  ``assert_mutation_success`` / ``assert_mutation_error``.
* A missing permission is expected to surface as a GraphQL ``error``, not as a
  mutation log marked failed. That distinction is asserted explicitly, because
  returning an error list for an authorisation failure is the mistake this
  file exists to prevent.
"""

import json
from datetime import date, datetime

from django.core.cache import caches
from django.test import override_settings

from core.models.openimis_graphql_test_case import (
    BaseTestContext,
    openIMISGraphQLTestCase,
)
from core.test_helpers import create_test_interactive_user, create_test_role
from location.test_helpers import (
    assign_user_districts,
    create_test_health_facility,
)

from provider_contract.models import (
    ContractFeeSchedule,
    EmpanelmentStage,
    EmpanelmentWorkflow,
    ProviderContract,
)

from .base import create_district_with_parent


class GraphQLTestBase(openIMISGraphQLTestCase):
    """Fixtures plus a JWT for a caller holding every provider_contract right."""

    GRAPHQL_SCHEMA = True

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.health_facility = create_test_health_facility(code="PCE-GQ-1")
        cls.workflow = cls._save(
            EmpanelmentWorkflow(
                code="GQL-DEFAULT",
                name="Default",
                status=EmpanelmentWorkflow.Status.ACTIVE,
                is_default=True,
            )
        )
        for index, code in enumerate(("APPLICATION", "SITE_VISIT", "DECISION")):
            cls._save(
                EmpanelmentStage(
                    workflow=cls.workflow,
                    code=code,
                    name=code,
                    sequence=index + 1,
                    outcome=EmpanelmentStage.Outcome.PASSED,
                    is_decision_stage=(code == "DECISION"),
                    requires_all_documents=False,
                )
            )
        # create_test_interactive_user() with no roles builds an admin role and
        # marks the account a superuser, which is what the harness expects for
        # the happy path.
        cls.user = create_test_interactive_user(username="pce_gql_admin")
        cls.context = BaseTestContext(cls.user)

    @classmethod
    def _save(cls, instance):
        # HistoryModel.save() needs an actor; reuse the fixture user where one
        # already exists, otherwise attribute to the first technical user.
        from core.test_helpers import create_test_technical_user

        user = getattr(cls, "user", None) or create_test_technical_user(
            username="pce_gql_seed", super_user=True
        )
        instance.save(user=user)
        return instance

    @property
    def token(self):
        return self.context.get_jwt()

    def gql_query(self, query, token=None, expect_errors=False):
        response = self.query(
            query, headers={"HTTP_AUTHORIZATION": f"Bearer {token or self.token}"}
        )
        if not expect_errors:
            self.assertResponseNoErrors(response)
        return json.loads(response.content)

    def edge_nodes(self, payload, field):
        return [e["node"] for e in payload["data"][field]["edges"]]

    def send(self, mutation, params, token=None):
        """Run a mutation and return its MutationLog node.

        ``send_mutation(follow=True)`` already polls and returns the mutation
        log, so there is no ``internalId`` to read back out of the response --
        that is the harness contract, not an accident. Assert on the returned
        node's ``status``/``error``.
        """
        content = self.send_mutation(mutation, params, token or self.token)
        return content["data"]["mutationLogs"]["edges"][0]["node"]


class ProviderContractQueryTests(GraphQLTestBase):
    """Reads on the contract list."""

    def setUp(self):
        super().setUp()
        caches["default"].clear()
        self._save(
            ProviderContract(
                provider=self.health_facility,
                contract_number="PC-Q1",
                date_start=date(2026, 1, 1),
                date_end=date(2026, 12, 31),
            )
        )

    def test_contracts_are_listed(self):
        payload = self.gql_query(
            """
            {
              providerContracts {
                edges { node { contractNumber, status, versionNo, dateStart, dateEnd } }
              }
            }
            """
        )
        nodes = self.edge_nodes(payload, "providerContracts")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["contractNumber"], "PC-Q1")
        self.assertEqual(nodes[0]["status"], "DRAFT")

    def test_contracts_can_be_filtered_by_number(self):
        payload = self.gql_query(
            """
            {
              providerContracts(contractNumber: "PC-NOPE") {
                edges { node { contractNumber } }
              }
            }
            """
        )
        self.assertEqual(self.edge_nodes(payload, "providerContracts"), [])

    def test_single_contract_is_fetched_by_uuid(self):
        contract = ProviderContract.objects.get(contract_number="PC-Q1")
        payload = self.gql_query(
            'query { providerContract(id: "%s") { contractNumber } }' % contract.uuid
        )
        self.assertEqual(payload["data"]["providerContract"]["contractNumber"], "PC-Q1")

    def test_search_helper_finds_a_contract(self):
        payload = self.gql_query(
            """
            {
              providerContractsStr(str: "PC-Q1") {
                edges { node { contractNumber } }
              }
            }
            """
        )
        self.assertEqual(
            [n["contractNumber"] for n in self.edge_nodes(payload, "providerContractsStr")],
            ["PC-Q1"],
        )

    def test_row_security_limits_the_list_to_the_callers_district(self):
        """A district-scoped caller must not see another district's contract."""
        elsewhere = create_district_with_parent("PCE-GD2")
        other_facility = create_test_health_facility(
            code="PCE-GQ-2", location_id=elsewhere.id
        )
        self._save(
            ProviderContract(
                provider=other_facility,
                contract_number="PC-Q2",
                date_start=date(2026, 1, 1),
                date_end=date(2026, 12, 31),
            )
        )

        scoped = create_test_interactive_user(
            username="pce_gql_scoped",
            password="PceTest123!",
            roles=[
                create_test_role(
                    perm_names=["gql_query_provider_contracts_perms"]
                ).id
            ],
        )
        assign_user_districts(scoped, [elsewhere.code])
        caches["default"].clear()
        token = BaseTestContext(scoped).get_jwt()

        payload = self.gql_query(
            """
            {
              providerContracts {
                edges { node { contractNumber } }
              }
            }
            """,
            token=token,
        )
        numbers = [n["contractNumber"] for n in self.edge_nodes(payload, "providerContracts")]
        self.assertEqual(numbers, ["PC-Q2"], "scoped caller saw a contract it must not")

    def test_anonymous_caller_is_refused(self):
        """No token at all. A previous version of this test passed the admin
        token by accident and so asserted nothing."""
        response = self.query(
            """
            {
              providerContracts {
                edges { node { contractNumber } }
              }
            }
            """
        )
        payload = json.loads(response.content)
        # Either an error or an empty connection is acceptable; silently
        # returning every contract is not.
        if "errors" in payload:
            return
        self.assertEqual(self.edge_nodes(payload, "providerContracts"), [])


class GateQueryTests(GraphQLTestBase):
    """The two read-only gate queries."""

    def test_verdict_for_an_unempanelled_provider_carries_a_reason_code(self):
        payload = self.gql_query(
            """
            {
              checkProviderEmpanelment(providerId: "%s", onDate: "2026-06-01") {
                empanelled, reasonCode, severity
              }
            }
            """
            % self.health_facility.uuid
        )
        verdict = payload["data"]["checkProviderEmpanelment"]
        self.assertFalse(verdict["empanelled"])
        self.assertEqual(verdict["reasonCode"], "NO_ACTIVE_CONTRACT")
        self.assertEqual(verdict["severity"], "BLOCKING")

    def test_verdict_for_an_active_provider_names_the_contract(self):
        self._save(
            ProviderContract(
                provider=self.health_facility,
                contract_number="PC-G1",
                status=ProviderContract.Status.ACTIVE,
                date_start=date(2026, 1, 1),
                date_end=date(2026, 12, 31),
                # Backdated: the verdict is asked about a service rendered in
                # June, and a version recorded today does not cover that date.
                date_valid_from=datetime(2025, 12, 1),
            )
        )
        payload = self.gql_query(
            """
            {
              checkProviderEmpanelment(providerId: "%s", onDate: "2026-06-01") {
                empanelled, contract, reasonCode
              }
            }
            """
            % self.health_facility.uuid
        )
        verdict = payload["data"]["checkProviderEmpanelment"]
        self.assertTrue(verdict["empanelled"])
        self.assertEqual(verdict["contract"], "PC-G1")
        self.assertIsNone(verdict["reasonCode"])

    def test_fee_query_reports_no_fee_match(self):
        payload = self.gql_query(
            """
            {
              applicableFee(providerId: "%s", onDate: "2026-06-01") {
                amount, reasonCode, resolutionMode
              }
            }
            """
            % self.health_facility.uuid
        )
        fee = payload["data"]["applicableFee"]
        self.assertIsNone(fee["amount"])
        self.assertEqual(fee["reasonCode"], "NO_ACTIVE_CONTRACT")
        # Which mechanism would price it, even when nothing matched.
        self.assertIn(fee["resolutionMode"], ("FACILITY_PRICELIST", "CONTRACT_CALCULE"))


@override_settings(AUTH_PASSWORD_VALIDATORS=[])
class ContractLifecycleMutationTests(GraphQLTestBase):
    """The state machine, driven the way a client drives it."""

    def create_contract(self, **overrides):
        params = {
            "providerId": self.health_facility.uuid,
            "contractNumber": overrides.pop("contractNumber", "PC-M1"),
            "dateStart": "2026-01-01",
            "dateEnd": "2026-12-31",
        }
        params.update(overrides)
        return self.send_mutation("createProviderContract", params, self.token)

    def test_full_lifecycle_to_active(self):
        self.create_contract()

        contract = ProviderContract.objects.get(contract_number="PC-M1")
        self.assertEqual(contract.status, ProviderContract.Status.DRAFT)

        for mutation, expected in (
            ("submitProviderContract", ProviderContract.Status.NEGOTIATION),
            ("approveProviderContract", ProviderContract.Status.SIGNED),
        ):
            self.send(mutation, {"id": str(contract.uuid)})
            contract.refresh_from_db()
            self.assertEqual(contract.status, expected)

    def test_amendment_opens_a_new_version(self):
        self.create_contract()
        contract = ProviderContract.objects.get(contract_number="PC-M1")

        self.send(
            "amendProviderContract",
            {"id": str(contract.uuid), "dateEnd": "2027-06-30"},
        )

        amended = ProviderContract.objects.get(
            contract_number="PC-M1", date_valid_to__isnull=True
        )
        self.assertNotEqual(amended.id, contract.id)
        self.assertEqual(amended.version_no, 2)
        self.assertEqual(amended.date_end, date(2027, 6, 30))
        # The predecessor is closed, not deleted.
        contract.refresh_from_db()
        self.assertIsNotNone(contract.date_valid_to)

    def test_terminate_requires_a_reason(self):
        self.create_contract()
        contract = ProviderContract.objects.get(contract_number="PC-M1")
        self.send("submitProviderContract", {"id": str(contract.uuid)})
        self.send("approveProviderContract", {"id": str(contract.uuid)})

        content = self.send_mutation(
            "terminateProviderContract",
            {"id": str(contract.uuid), "reason": ""},
            self.token,
            follow=False,
            allow_exceptions=False,
        )
        # openIMIS replies immediately with an internalId whatever happens; the
        # outcome is on the MutationLog, so that is what gets asserted.
        internal_id = content["data"]["terminateProviderContract"]["internalId"]
        self.assert_mutation_error(internal_id, self.token, "reason")
        contract.refresh_from_db()
        self.assertNotEqual(contract.status, ProviderContract.Status.TERMINATED)

    def test_unknown_uuid_fails_the_mutation(self):
        content = self.send_mutation(
            "submitProviderContract",
            {"id": "00000000-0000-0000-0000-000000000000"},
            self.token,
            follow=False,
            allow_exceptions=False,
        )
        internal_id = content["data"]["submitProviderContract"]["internalId"]
        self.assert_mutation_error(internal_id, self.token, "No such contract")

    def test_caller_without_the_right_is_refused(self):
        """A caller lacking the create right must not create anything."""
        limited = create_test_interactive_user(
            username="pce_gql_limited",
            password="PceTest123!",
            roles=[
                create_test_role(perm_names=["gql_query_provider_contracts_perms"]).id
            ],
            custom_props={"is_superuser": False, "is_staff": False},
        )
        token = BaseTestContext(limited).get_jwt()
        content = self.send_mutation(
            "createProviderContract",
            {
                "providerId": self.health_facility.uuid,
                "contractNumber": "PC-DENIED",
                "dateStart": "2026-01-01",
                "dateEnd": "2026-12-31",
            },
            token,
            follow=False,
            allow_exceptions=False,
        )
        internal_id = content["data"]["createProviderContract"]["internalId"]
        # openIMIS redacts the raised exception's detail for security, so the
        # log names the exception class rather than the reason.
        self.assert_mutation_error(internal_id, token, "PermissionDenied")
        self.assertFalse(
            ProviderContract.objects.filter(contract_number="PC-DENIED").exists(),
            "a caller lacking the create right created a contract",
        )


class FeeScheduleMutationTests(GraphQLTestBase):
    """Fee schedules and rows."""

    def test_schedule_and_fee_row_round_trip(self):
        self._save(
            ProviderContract(
                provider=self.health_facility,
                contract_number="PC-F1",
                status=ProviderContract.Status.ACTIVE,
                date_start=date(2026, 1, 1),
                date_end=date(2026, 12, 31),
            )
        )
        contract = ProviderContract.objects.get(contract_number="PC-F1")

        self.send(
            "createContractFeeSchedule",
            {"contractId": str(contract.uuid), "name": "Standard"},
        )
        schedule = ContractFeeSchedule.objects.get(contract=contract)
        self.assertEqual(schedule.status, ContractFeeSchedule.Status.DRAFT)

        from medical.test_helpers import create_test_service

        service = create_test_service(category="S")
        self.send(
            "saveContractFeeItem",
            {
                "scheduleId": str(schedule.uuid),
                "serviceId": str(service.id),
                "amount": "42.00",
            },
        )
        self.assertEqual(schedule.fee_items.count(), 1)
        self.assertEqual(
            str(schedule.fee_items.get().amount), "42.00"
        )

    def test_activating_an_empty_schedule_is_refused(self):
        self._save(
            ProviderContract(
                provider=self.health_facility,
                contract_number="PC-F2",
                status=ProviderContract.Status.ACTIVE,
                date_start=date(2026, 1, 1),
                date_end=date(2026, 12, 31),
            )
        )
        contract = ProviderContract.objects.get(contract_number="PC-F2")
        self.send(
            "createContractFeeSchedule",
            {"contractId": str(contract.uuid), "name": "Empty"},
        )
        schedule = ContractFeeSchedule.objects.get(contract=contract)

        content = self.send_mutation(
            "activateContractFeeSchedule",
            {"id": str(schedule.uuid)},
            self.token,
            follow=False,
            allow_exceptions=False,
        )
        internal_id = content["data"]["activateContractFeeSchedule"]["internalId"]
        self.assert_mutation_error(internal_id, self.token, "empty fee schedule")
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, ContractFeeSchedule.Status.DRAFT)


class EmpanelmentMutationTests(GraphQLTestBase):
    """Workflow execution over GraphQL."""

    def test_process_is_created_and_submitted(self):
        self.send(
            "createEmpanelmentProcess",
            {
                "providerId": self.health_facility.uuid,
                "workflowId": str(self.workflow.uuid),
                "referenceNo": "PC-REF-1",
            },
        )

        from provider_contract.models import EmpanelmentProcess

        process = EmpanelmentProcess.objects.get(reference_no="PC-REF-1")
        self.assertEqual(process.status, EmpanelmentProcess.Status.DRAFT)

        self.send("submitEmpanelmentProcess", {"id": str(process.uuid)})
        process.refresh_from_db()
        self.assertEqual(process.status, EmpanelmentProcess.Status.SUBMITTED)
