import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

MODULE_NAME = "calcrule_provider_contract_scope"


def register_calculation_rules(rules=None):
    """Add this module's rule to the framework's registry.

    ``calculation.apps.CALCULATION_RULES`` is the single list every
    ``calculation.services`` helper iterates, and
    ``calculation.apps.read_all_calculation_rules`` only imports rules from the
    ``calculation`` package itself. A rule living in its own module therefore
    has to append itself here.

    Guarded so that importing this app twice -- which ``ready()`` makes easy to
    do under some test runners -- cannot register the same rule twice. A
    duplicate would make the gate evaluate every claim line twice, and while
    recording is idempotent, doing the work twice is still waste.
    """
    from .calculation_rule import ProviderContractScopeRule

    if rules is None:
        from calculation.apps import CALCULATION_RULES

        rules = CALCULATION_RULES

    if ProviderContractScopeRule in rules:
        return rules

    ProviderContractScopeRule.ready()
    rules.append(ProviderContractScopeRule)
    logger.debug("registered calculation rule %s", ProviderContractScopeRule.uuid)
    return rules


class CalcruleProviderContractScopeConfig(AppConfig):
    name = MODULE_NAME

    def ready(self):
        register_calculation_rules()
