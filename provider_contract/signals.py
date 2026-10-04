"""Service-signal bindings for the provider_contract module.

The openIMIS assembly crawls every installed module looking for a
``bind_service_signals`` callable in ``<module>/signals.py`` and invokes it
after all apps are loaded. That two-phase dance exists because
:func:`core.signals.register_service_signal` only registers a signal once the
decorated class body has been imported, so a receiver connected eagerly can
land before its signal exists. ``core.service_signals.RegisteredServiceSignal``
queues such connections and drains them on registration.

Bindings currently registered by this module:

``update_provider_contract``
    Fires before and after a contract mutation. Used by the service layer to
    keep ``location.HealthFacility.contract_start_date`` /
    ``contract_end_date`` in step with the contract that supersedes those
    legacy columns (ADR-003).

``check_provider_empanelment``
    Fires after a gate evaluation. Reserved for scheme-specific extensions
    (additional accreditation requirements, regional blacklists).

``applicable_fee``
    Fires after fee resolution so a scheme can layer an override on top of the
    contract's fee schedule without forking the engine.
"""


def bind_service_signals():
    # Bound in the services phase; the receivers below intentionally do not exist
    # yet and binding a missing receiver would fail at start-up.
    #
    # from core.signals import bind_service_signal
    #
    # bind_service_signal("update_provider_contract", on_contract_updated)
    # bind_service_signal("check_provider_empanelment", on_gate_result)
    # bind_service_signal("applicable_fee", on_fee_resolved)
    return None
