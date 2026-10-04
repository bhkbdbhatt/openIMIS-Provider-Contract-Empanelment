"""The calculation rule itself.

openIMIS' calculation framework is class-based: a rule is a subclass of
:class:`core.abs_calculation_rule.AbsStrategy` carrying declarative metadata,
which makes it visible in the ``calculationRules`` GraphQL query and therefore
administrable rather than hard-wired.

The important property of everything below is that it is **advisory**. This rule
never raises into claim processing, never modifies a claim, and is completely
inert when ``pce_gate_enabled`` is False (ADR-006). A gate finding is a row in
``ClaimScopeViolation`` and nothing else; the claim carries on exactly as it
would have without this module installed.

Why a rule *and* a signal: the claim module does not invoke
``calculation.services.run_calculation_rules`` anywhere, so a rule alone would
never fire during claim valuation. :mod:`.signals` binds the same
:func:`evaluate_claim` entry point to ``claim.claim_valuated``. There is one
implementation, two ways in.
"""

import logging

from core.abs_calculation_rule import AbsStrategy
from core import datetime

from claim.models import Claim, ClaimService

from .gate import evaluate_claim, gate_is_enabled

logger = logging.getLogger(__name__)


class ProviderContractScopeRule(AbsStrategy):
    """Provider scope as an openIMIS calculation rule.

    ``impacted_class_parameter`` targets ``ClaimService`` because that is the
    object a scope question is actually asked about: a claim can be entirely
    covered by one contract and partly outside it. ``AbsStrategy`` walks the
    foreign keys of the impacted class looking for a matching entry, so naming
    the service is what makes the rule reachable from a claim.
    """

    version = 1
    # Stable identity: clients store this uuid against a calculation. Do not
    # regenerate it once released.
    uuid = "8c1f5f0a-4d2b-4f6e-9a3c-1b7d5e2f0a91"
    calculation_rule_name = "Provider contract scope"
    description = (
        "Checks that the treating facility held a contract covering the service "
        "on the date of service, and records a ClaimScopeViolation when it did "
        "not. Advisory: the claim is never rejected, amended or cancelled."
    )
    date_valid_from = datetime.datetime(2000, 1, 1)
    date_valid_to = None
    status = "active"
    type = "provider_contract"
    sub_type = "scope"
    supports_advanced_criteria = True
    # No conversion between calculation classes is offered: this rule prices
    # nothing itself, it only observes and records.
    from_to = []
    impacted_class_parameter = [
        {
            "class": "ClaimService",
            "parameters": [
                {
                    "type": "boolean",
                    "name": "enforce_contract_scope",
                    "label": {"en": "Flag services outside the provider contract"},
                    "rights": {"read": [], "write": [], "update": [], "replace": []},
                    "optionSet": [
                        {"value": "true", "label": {"en": "Flag", "fr": ""}},
                        {"value": "false", "label": {"en": "Ignore", "fr": ""}},
                    ],
                    "default": "true",
                    "relevance": "",
                }
            ],
        }
    ]

    @classmethod
    def check_calculation(cls, instance):
        """Does this rule apply to ``instance`` at all?

        True only for a claim service that names a provider and a service, and
        only while the gate is enabled. Returning True for an instance with no
        provider would make every unrelated claim in the instance pay for a
        lookup.
        """
        if not gate_is_enabled():
            return False
        return bool(
            isinstance(instance, ClaimService)
            and getattr(instance, "claim_id", None)
            and getattr(instance, "service_id", None)
        )

    @classmethod
    def active_for_object(cls, instance, context=None, type=None, sub_type=None):
        """Whether to actually run for this object right now.

        The gate is deliberately active in both fee-resolution modes: in
        ``FACILITY_PRICELIST`` the flagging is the whole point, and in
        ``CONTRACT_CALCULE`` this rule is also where the contracted price comes
        from.
        """
        if not gate_is_enabled():
            return False
        if type is not None and type != cls.type:
            return False
        if sub_type is not None and sub_type != cls.sub_type:
            return False
        if context is not None and context not in ("claim", "claim_service", None):
            return False
        return cls.check_calculation(instance)

    @classmethod
    def calculate(cls, instance, *args, **kwargs):
        """Run the gate for one claim service and return what was found.

        Returns a summary dict rather than mutating anything. Any unexpected
        exception is swallowed and logged: a valuation path must not be taken
        down by a reference module.
        """
        try:
            return evaluate_claim(instance.claim, user=kwargs.get("user"))
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "provider contract scope rule failed; claim is being valued "
                "without it, which is the advisory behaviour"
            )
            return None

    @classmethod
    def convert(cls, instance, convert_to, **kwargs):
        """This rule prices nothing, so there is nothing to convert."""
        return None


# The framework discovers rules through ``calculation.apps.CALCULATION_RULES``.
# Exporting the name here as well keeps ``from .calculation_rule import *``-style
# imports in downstream tooling working.
RULES = [ProviderContractScopeRule]

__all__ = ["Claim", "ProviderContractScopeRule", "RULES"]
