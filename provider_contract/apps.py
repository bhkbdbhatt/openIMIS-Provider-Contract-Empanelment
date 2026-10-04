from django.apps import AppConfig

MODULE_NAME = "provider_contract"


# Permission right blocks:
#   1530xx  module administration (empanelment criteria, workflow templates)
#   1531xx  empanelment workflow
#   1532xx  contract lifecycle
#   1533xx  fee schedules
#   1534xx  claims gate / scope violations
DEFAULT_CFG = {
    # --- behaviour flags -------------------------------------------------
    # When False the claims gate is inert: every claim prices and validates as
    # if this module were not installed.
    "pce_gate_enabled": True,
    # "flag_only"      -> record a ClaimScopeViolation, leave the claim untouched
    # "route_to_review" -> additionally push BLOCKING claims into the claim
    #                     module's Review stage
    "pce_gate_mode": "flag_only",
    "pce_gate_cache_ttl_seconds": 300,
    # "FACILITY_PRICELIST" -> materialize fee rows into a dedicated
    #     medical_pricelist.ServicesPricelist and assign it to
    #     HealthFacility.services_pricelist. Requires exactly one ACTIVE
    #     contract per facility. Reuses openIMIS claim valuation unchanged.
    # "CONTRACT_CALCULE"   -> leave HealthFacility.services_pricelist alone and
    #     resolve ContractFeeItem per (provider, service, date) in the
    #     calcrule. Supports any number of concurrent contracts per facility.
    "pce_fee_resolution_mode": "FACILITY_PRICELIST",
    # --- defaults --------------------------------------------------------
    "pce_default_renewal_window_days": 60,
    "pce_default_grace_period_days": 30,
    # Filesystem root for uploaded empanelment documents. None keeps document
    # bytes in the database (base64), which is the openIMIS default.
    "pce_attachment_root_path": None,
    # --- permissions -----------------------------------------------------
    "gql_mutation_pce_admin_perms": ["153001"],
    "gql_query_empanelment_processes_perms": ["153101"],
    "gql_mutation_create_empanelment_process_perms": ["153102"],
    "gql_mutation_submit_empanelment_process_perms": ["153103"],
    "gql_mutation_advance_empanelment_process_perms": ["153104"],
    "gql_mutation_decide_empanelment_process_perms": ["153105"],
    "gql_mutation_withdraw_empanelment_process_perms": ["153106"],
    "gql_mutation_add_empanelment_document_perms": ["153107"],
    "gql_mutation_verify_empanelment_document_perms": ["153108"],
    "gql_mutation_reject_empanelment_document_perms": ["153109"],
    "gql_mutation_update_empanelment_process_perms": ["153110"],
    "gql_mutation_delete_empanelment_process_perms": ["153111"],
    "gql_query_provider_contracts_perms": ["153201"],
    "gql_mutation_create_provider_contract_perms": ["153202"],
    "gql_mutation_update_provider_contract_perms": ["153203"],
    "gql_mutation_delete_provider_contract_perms": ["153204"],
    "gql_mutation_submit_provider_contract_perms": ["153205"],
    "gql_mutation_approve_provider_contract_perms": ["153206"],
    "gql_mutation_amend_provider_contract_perms": ["153207"],
    "gql_mutation_renew_provider_contract_perms": ["153208"],
    "gql_mutation_terminate_provider_contract_perms": ["153209"],
    "gql_query_contract_fee_schedules_perms": ["153301"],
    "gql_mutation_create_contract_fee_schedule_perms": ["153302"],
    "gql_mutation_activate_contract_fee_schedule_perms": ["153303"],
    "gql_mutation_create_contract_fee_item_perms": ["153304"],
    "gql_mutation_update_contract_fee_item_perms": ["153305"],
    "gql_mutation_delete_contract_fee_item_perms": ["153306"],
    "gql_mutation_bulk_update_fees_perms": ["153307"],
    "gql_query_claim_scope_violations_perms": ["153401"],
    "gql_mutation_resolve_claim_scope_violation_perms": ["153402"],
    "gql_mutation_waive_claim_scope_violation_perms": ["153403"],
}


class ProviderContractConfig(AppConfig):
    name = MODULE_NAME
    verbose_name = "Provider Contract & Empanelment"

    # --- behaviour flags ---
    pce_gate_enabled = True
    pce_gate_mode = "flag_only"
    pce_gate_cache_ttl_seconds = 300
    pce_fee_resolution_mode = "FACILITY_PRICELIST"
    # --- defaults ---
    pce_default_renewal_window_days = 60
    pce_default_grace_period_days = 30
    pce_attachment_root_path = None
    # --- permissions ---
    gql_mutation_pce_admin_perms = []
    gql_query_empanelment_processes_perms = []
    gql_mutation_create_empanelment_process_perms = []
    gql_mutation_submit_empanelment_process_perms = []
    gql_mutation_advance_empanelment_process_perms = []
    gql_mutation_decide_empanelment_process_perms = []
    gql_mutation_withdraw_empanelment_process_perms = []
    gql_mutation_add_empanelment_document_perms = []
    gql_mutation_verify_empanelment_document_perms = []
    gql_mutation_reject_empanelment_document_perms = []
    gql_mutation_update_empanelment_process_perms = []
    gql_mutation_delete_empanelment_process_perms = []
    gql_query_provider_contracts_perms = []
    gql_mutation_create_provider_contract_perms = []
    gql_mutation_update_provider_contract_perms = []
    gql_mutation_delete_provider_contract_perms = []
    gql_mutation_submit_provider_contract_perms = []
    gql_mutation_approve_provider_contract_perms = []
    gql_mutation_amend_provider_contract_perms = []
    gql_mutation_renew_provider_contract_perms = []
    gql_mutation_terminate_provider_contract_perms = []
    gql_query_contract_fee_schedules_perms = []
    gql_mutation_create_contract_fee_schedule_perms = []
    gql_mutation_activate_contract_fee_schedule_perms = []
    gql_mutation_create_contract_fee_item_perms = []
    gql_mutation_update_contract_fee_item_perms = []
    gql_mutation_delete_contract_fee_item_perms = []
    gql_mutation_bulk_update_fees_perms = []
    gql_query_claim_scope_violations_perms = []
    gql_mutation_resolve_claim_scope_violation_perms = []
    gql_mutation_waive_claim_scope_violation_perms = []

    def __load_config(self, cfg):
        for field in cfg:
            if hasattr(ProviderContractConfig, field):
                setattr(ProviderContractConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)

    def set_dataloaders(self, dataloaders):
        # Populated in the GraphQL phase; the assembly calls this hook once
        # every module's AppConfig has been loaded.
        return None
