"""Schema entry point for the calcrule module.

The module exposes no queries or mutations of its own: it contributes one
calculation rule and binds one receiver. ``bind_signals`` is what the openIMIS
assembly looks for when it collects a module's startup hooks, so it has to be
re-exported here even though there is nothing else for the assembly to pick up.
"""

from .signals import bind_service_signals

bind_signals = bind_service_signals

__all__ = ["bind_signals"]
