"""Where the gate actually runs during claim processing.

The claim module does not call ``calculation.services.run_calculation_rules``,
so a calculation rule alone would never fire while a claim is valued. This
module binds the same entry point to ``claim.claim_valuated``, the signal openIMIS
emits once a claim has been valued.

Two properties matter more than anything else here:

* **The claim is not touched.** The receiver reads the claim and records
  findings about it. It assigns nothing, saves nothing, and returns no errors
  (ADR-006).
* **A failure here must not fail the claim.** ``claim.claim_valuated`` is sent
  from inside claim valuation, and a raised exception would reject the claim
  for a reason that belongs in a review queue. Every path is therefore wrapped
  so the worst case is a missing flag.

Binding is deferred to :func:`bind_service_signals` because
``register_service_signal`` only creates the signal when the decorated function
is imported, and a receiver connected at import time can land before the signal
exists.
"""

import logging

from core.signals import bind_service_signal

from .gate import evaluate_claim, gate_is_enabled

logger = logging.getLogger(__name__)


def on_claim_valuated(sender, claim=None, errors=None, user=None, **kwargs):
    """Apply the provider contract scope gate to a freshly valued claim.

    ``sender`` is the claim service class and ``claim``/``errors``/``user`` are
    the signal's providing arguments, matching openIMIS' signature.

    Returns the gate summary so a caller that invokes it directly can see what
    happened. Returning a non-empty list here would be read as validation
    errors by anything that sends this signal, so failures are never returned;
    they are logged.
    """
    if claim is None:
        return None
    if not gate_is_enabled():
        return None
    try:
        summary = evaluate_claim(claim, user=user)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "provider contract scope gate failed for claim %s; the claim is "
            "being valued without it, which is the intended fallback",
            getattr(claim, "code", "?"),
        )
        return None
    if summary and summary.get("violations"):
        logger.info(
            "claim %s priced outside the provider contract scope: %s",
            getattr(claim, "code", "?"),
            summary.get("results"),
        )
    return summary


def bind_service_signals():
    """Connect the receivers. Called by the assembly once apps are loaded."""
    bind_service_signal("claim.claim_valuated", on_claim_valuated)
    logger.debug("provider contract scope: bound claim.claim_valuated")
    return None
