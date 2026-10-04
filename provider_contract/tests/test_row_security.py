"""District-level row security for every owned model.

openIMIS enforces data isolation at the queryset layer, so a model with a wrong
``location_prefix`` leaks contracts and fee schedules across districts while
still looking correct in every other test. Two things are therefore checked
here:

* structurally, that each ``location_prefix`` really walks the foreign key path
  all the way to ``location.Location`` -- a pure metadata assertion that needs
  no fixtures and cannot rot silently;
* behaviourally, that a caller scoped to one district cannot see another
  district's contracts.
"""

from django.apps import apps as django_apps
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.test import override_settings

from core.models import User
from core.test_helpers import create_test_interactive_user, create_test_role
from location.models import Location
from location.test_helpers import (
    assign_user_districts,
    create_test_health_facility,
)

from provider_contract.models import EMPTY_UUID, ContractFeeItem, ProviderContract

from .base import ProviderContractTestCase, create_district_with_parent


def owned_models():
    return [
        model
        for model in django_apps.get_app_config("provider_contract").get_models()
        if getattr(model, "location_prefix", None)
    ]


class LocationPrefixTests(ProviderContractTestCase):
    """The prefix must reach location.Location, not the provider."""

    def resolve_prefix(self, model):
        field = None
        for part in model.location_prefix.split("__"):
            field = model._meta.get_field(part) if field is None else field.related_model._meta.get_field(part)
        return field.related_model

    def test_every_owned_model_declares_a_prefix(self):
        self.assertTrue(owned_models(), "no owned models declare location_prefix")

    def test_prefix_resolves_to_location(self):
        for model in owned_models():
            with self.subTest(model=model.__name__):
                self.assertIs(
                    self.resolve_prefix(model),
                    Location,
                    f"{model.__name__}.location_prefix "
                    f"({model.location_prefix!r}) does not reach location.Location",
                )

    def test_prefix_names_the_fk_chain_not_the_provider(self):
        """Guards the specific mistake: comparing facilities against location ids."""
        for model in owned_models():
            with self.subTest(model=model.__name__):
                self.assertNotEqual(model.location_prefix, "provider")
                self.assertNotEqual(model.location_prefix, "provider__location__id")

    def test_deeply_nested_models_declare_their_full_path(self):
        # The deepest model needs five hops to reach Location; getting this
        # wrong is invisible until a fee row leaks across a district boundary.
        self.assertEqual(
            ContractFeeItem.location_prefix,
            "fee_schedule__contract__provider__location",
        )


@override_settings(ROW_SECURITY=True)
class RowSecurityBehaviourTests(ProviderContractTestCase):
    def setUp(self):
        super().setUp()
        caches["default"].clear()
        self.other_district = create_district_with_parent("PCE-D2")
        self.home_district = self.health_facility.location

    def district_user(self, username, district_codes, super_user=False):
        # A real DEFAULT_CFG permission, not a wildcard: openIMIS grants rights
        # by explicit right id, so there is no "*" to resolve.
        user = create_test_interactive_user(
            username=username,
            # openIMIS enforces complexity on real user creation, and the
            # helper's "admin123" default trips the uppercase rule.
            password="PceTest123!",
            roles=[
                create_test_role(
                    perm_names=["gql_query_provider_contracts_perms"]
                ).id
            ],
            custom_props={"is_superuser": super_user, "is_staff": super_user},
        )
        assign_user_districts(user, district_codes)
        caches["default"].clear()
        return user

    def test_anonymous_caller_sees_nothing(self):
        """Anonymous access must yield an empty set, not an unfiltered one."""
        self.build_contract()
        visible = ProviderContract.get_queryset(
            ProviderContract.objects.all(), AnonymousUser()
        )
        self.assertEqual(list(visible), [])
        self.assertEqual(ProviderContract.objects.count(), 1)

    def test_empty_uuid_is_not_a_valid_location_id(self):
        """The sentinel must be a nil UUID: id=-1 is invalid on a UUID pk."""
        self.assertEqual(EMPTY_UUID, "00000000-0000-0000-0000-000000000000")

    def test_caller_scoped_elsewhere_cannot_see_the_contract(self):
        self.build_contract()
        outsider = self.district_user("pce_outsider", [self.other_district.code])

        visible = ProviderContract.get_queryset(
            ProviderContract.objects.all(), outsider
        )

        self.assertEqual(list(visible), [])

    def test_caller_scoped_here_can_see_the_contract(self):
        contract = self.build_contract()
        insider = self.district_user("pce_insider", [self.home_district.code])

        visible = ProviderContract.get_queryset(ProviderContract.objects.all(), insider)

        self.assertEqual([c.id for c in visible], [contract.id])

    def test_superuser_sees_every_district(self):
        self.build_contract()
        admin = self.district_user("pce_admin", [], super_user=True)

        visible = ProviderContract.get_queryset(ProviderContract.objects.all(), admin)

        self.assertEqual(visible.count(), 1)

    def test_core_user_is_also_accepted(self):
        """Some call sites pass core.User rather than InteractiveUser."""
        self.build_contract()
        user = create_test_interactive_user(
            username="pce_core_user",
            password="PceTest123!",
            roles=[
                create_test_role(
                    perm_names=["gql_query_provider_contracts_perms"]
                ).id
            ],
            custom_props={"is_superuser": False, "is_staff": False},
        )
        assign_user_districts(user, [self.other_district.code])
        caches["default"].clear()
        # The auth principal as a view layer would really pass it: the
        # core.User, not the InteractiveUser behind it.
        core_user = User.objects.filter(i_user_id=user.id).first()

        visible = ProviderContract.get_queryset(ProviderContract.objects.all(), core_user)

        self.assertEqual(
            list(visible), [], "passing core.User must not disable row security"
        )

    def test_row_security_off_returns_everything(self):
        self.build_contract()
        outsider = self.district_user("pce_no_sec", [self.other_district.code])

        with override_settings(ROW_SECURITY=False):
            visible = ProviderContract.get_queryset(
                ProviderContract.objects.all(), outsider
            )

        self.assertEqual(visible.count(), 1)


@override_settings(ROW_SECURITY=True)
class FeeRowSecurityTests(ProviderContractTestCase):
    """A fee row must be as isolated as the contract it belongs to."""

    def setUp(self):
        super().setUp()
        caches["default"].clear()
        self.other_district = create_district_with_parent("PCE-D3")
        self.outsider = create_test_interactive_user(
            username="pce_fee_outsider",
            password="PceTest123!",
            roles=[
                create_test_role(
                    perm_names=["gql_query_provider_contracts_perms"]
                ).id
            ],
            custom_props={"is_superuser": False, "is_staff": False},
        )
        assign_user_districts(self.outsider, [self.other_district.code])
        caches["default"].clear()

    def test_fee_item_inherits_isolation_through_its_schedule(self):
        from medical.test_helpers import create_test_service

        self.build_fee_item(service=create_test_service(category="S"))

        visible = ContractFeeItem.get_queryset(
            ContractFeeItem.objects.all(), self.outsider
        )

        self.assertEqual(list(visible), [])

    def test_unrelated_facility_is_not_inherited(self):
        elsewhere = create_test_health_facility(
            code="PCE-HF-O",
            location_id=self.other_district.id,
        )
        self.build_contract(provider=elsewhere)

        visible = ProviderContract.get_queryset(
            ProviderContract.objects.all(), self.outsider
        )

        self.assertEqual(
            [c.id for c in visible],
            [ProviderContract.objects.get(provider=elsewhere).id],
            "the user is scoped to this district and should see its contract",
        )

    def test_fee_item_of_a_visible_contract_is_visible(self):
        from medical.test_helpers import create_test_service

        elsewhere = create_test_health_facility(
            code="PCE-HF-O",
            location_id=self.other_district.id,
        )
        self.build_fee_item(
            fee_schedule=self.build_fee_schedule(
                contract=self.build_contract(provider=elsewhere)
            ),
            service=create_test_service(category="S"),
        )

        visible = ContractFeeItem.get_queryset(
            ContractFeeItem.objects.all(), self.outsider
        )

        self.assertEqual(visible.count(), 1)
