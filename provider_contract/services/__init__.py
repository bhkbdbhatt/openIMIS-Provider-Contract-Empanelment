"""Business logic for provider empanelment and contracts.

GraphQL mutations call into these services; nothing else should write to the
module's models. The split is:

* :mod:`.contract_lifecycle` -- the contract state machine, amendment versioning
  and the service-enforced uniqueness rules (ADR-002).
* :mod:`.empanelment` -- workflow execution and the document checklist.
* :mod:`.fees` -- fee schedules, fee rows, and projection into openIMIS
  pricelists (ADR-004).
* :mod:`.violations` -- the read-only claims gate and its queue (ADR-006).
* :mod:`.common` -- configuration access and the dated lookups they share.
"""

from .common import (
    active_contract_for,
    as_date,
    as_datetime,
    contracts_covering,
    current_active_contract,
    effective_fee_schedule,
    get_config,
    resolve_fee,
    valid_at,
)
from .contract_lifecycle import (
    ProviderContractService,
    contracts_expiring_within,
    contracts_past_end_date,
    expire_contracts,
)
from .empanelment import EmpanelmentService
from .fees import ContractFeeService, fee_for
from .violations import (
    ClaimScopeViolationService,
    applicable_fee,
    check_provider_empanelment,
    gate_enabled,
)

__all__ = [
    "ClaimScopeViolationService",
    "ContractFeeService",
    "EmpanelmentService",
    "ProviderContractService",
    "active_contract_for",
    "applicable_fee",
    "as_date",
    "as_datetime",
    "check_provider_empanelment",
    "contracts_covering",
    "contracts_expiring_within",
    "contracts_past_end_date",
    "current_active_contract",
    "effective_fee_schedule",
    "expire_contracts",
    "fee_for",
    "gate_enabled",
    "get_config",
    "resolve_fee",
    "valid_at",
]
