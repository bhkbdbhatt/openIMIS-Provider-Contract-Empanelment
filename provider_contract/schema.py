"""Schema entry point for provider_contract.

The openIMIS assembly discovers a module's GraphQL surface by importing
``<app>.schema`` and, if present, collecting its ``Query`` and ``Mutation``
classes into the root schema (see ``openIMIS/schema.py``). This file exists to
be that import target; the substance lives in :mod:`provider_contract.gql`.

``bind_signals`` is also collected by the assembly, which is how
:func:`provider_contract.signals.bind_service_signals` gets called.
"""

from .gql import Mutation, Query
from .signals import bind_service_signals

__all__ = ["Mutation", "Query", "bind_signals"]

#: The assembly calls this once every app has loaded. It is a thin alias of the
#: module's own binder so that the assembly has a single, uniform hook name.
bind_signals = bind_service_signals
