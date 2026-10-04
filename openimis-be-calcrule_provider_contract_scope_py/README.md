# openIMIS calculation rule — provider contract scope

An openIMIS Backend [calculation rule][calc] that asks, while a claim is being
priced, whether the treating facility actually held a contract covering the
service on the date it was rendered.

**It flags. It never rejects.** A finding is a row in
`ClaimScopeViolation` and nothing else: the claim is never cancelled, amended,
rejected or routed to review by this module. See [ADR-006](#advisory-by-design)
for why that is a hard rule rather than a default.

This is a small module on purpose. All of the contract and empanelment logic
lives in [`openimis-be-provider_contract_py`][pce]; this one adapts that logic
to the claim side and owns no tables.

## What it does

| Situation | Result |
|---|---|
| `pce_gate_enabled = False` | Completely inert. No lookup, no row, no cost. |
| Provider empanelled, service in scope | Nothing recorded |
| Provider not empanelled | `ClaimScopeViolation`, severity `BLOCKING` |
| Contract had ended by the date of service | `ClaimScopeViolation`, severity `BLOCKING` |
| Contracted but no fee row for the service | `ClaimScopeViolation`, severity `WARNING` |
| `CONTRACT_CALCULE` mode | Also returns the contracted price via `resolve_fee_for` |
| A claim with no treating facility | Skipped — there is nothing to scope |
| Anything raises internally | Logged and swallowed; the claim still prices |

## Install

Add it to your assembly's module manifest alongside `provider_contract`:

```json
{
  "name": "calcrule_provider_contract_scope",
  "pip": "git+https://github.com/openimis/openimis-be-calcrule_provider_contract_scope_py.git@develop#egg=openimis-be-calcrule-provider-contract-scope"
}
```

It must be installed **after** `provider_contract`, `claim` and `calculation`,
because `ready()` registers the rule with the framework.

No migrations. The module owns no models.

## How it runs

openIMIS calculation rules are class-based: a rule is a subclass of
`core.abs_calculation_rule.AbsStrategy` carrying declarative metadata, which is
what makes it visible and administrable through the `calculationRules` GraphQL
query rather than hard-wired.

The subtlety worth knowing: **the claim module never calls
`calculation.services.run_calculation_rules`.** A rule on its own would
therefore never fire during claim valuation. So there are two entry points onto
one implementation:

| Entry point | Fires when | Used by |
|---|---|---|
| `ProviderContractScopeRule.calculate()` | the calculation framework runs rules | scheme-specific customisations, the GraphQL `calculationRules` view |
| `signals.on_claim_valuated` | `claim.claim_valuated` is sent | production claim processing |

Binding is deferred to `bind_service_signals()`, because
`core.signals.register_service_signal` only creates a signal when the decorated
function has been imported, and a receiver connected earlier would find nothing
to connect to.

## API

Nothing GraphQL-exposed of its own; the rule shows up in `calculationRules`.

```python
from calcrule_provider_contract_scope.gate import evaluate_claim, resolve_fee_for

# Flag any scope breaches on a claim. Returns a summary, records violations.
summary = evaluate_claim(claim, user=user)
# {"checked": 3, "violations": 1, "resolution_mode": "CONTRACT_CALCULE", "results": [...]}

# The contracted price for one line, for CONTRACT_CALCULE deployments.
fee = resolve_fee_for(claim, service=service, user=user)
# {"amount": Decimal("42.00"), "contract": "PC-2026-001", ...}
```

### `resolve_fee_for` is not wired up

Flagging works today: `claim.claim_valuated` fires and findings are recorded.

Pricing does **not**. The claim module prices from
`HealthFacility.services_pricelist` and never consults a calculation rule, so
`CONTRACT_CALCULE` mode still needs a one-line change in claim valuation to
route pricing through `resolve_fee_for`. That is deliberately left to the
deploying scheme — it changes the price of every claim, which is not a change a
reference module should make on its own. Until then, `applicable_fee` over the
GraphQL API is how an adjudicator sees a contracted fee.

Set `pce_fee_resolution_mode` to `FACILITY_PRICELIST` (the default) and this
does not matter: fees are materialized into the facility pricelist at
activation, and this module only flags.

## Configuration

Read from `provider_contract`'s configuration; nothing new here.

| Key | Default | Effect on this rule |
|---|---|---|
| `pce_gate_enabled` | `True` | `False` makes the module fully inert |
| `pce_gate_mode` | `flag_only` | This module never routes claims to Review; that is the deferred PR's job |
| `pce_fee_resolution_mode` | `FACILITY_PRICELIST` | Only affects flagging today; pricing still comes from the facility pricelist |

## Advisory by design

A gate that rejects claims in the valuation path will, sooner or later, reject
a claim for a reason nobody can reconstruct. So:

- nothing here mutates a claim, and the test suite asserts that by comparing
  every field of the claim before and after a gate run;
- every internal failure is swallowed — a missing flag is regrettable, a claim
  that fails to price because of a reference module is not;
- `BLOCKING` is a *severity on a queue row*, not an instruction. Nothing in this
  module reads it.

See ADR-006 in the provider contract module's `AGENTS.md`.

## Tests

```bash
cd openIMIS
OPENIMIS_CONF=../openimis-dev.json python manage.py test --keepdb calcrule_provider_contract_scope
```

Fixtures are reused from `provider_contract.tests.base`, deliberately: the two
modules have to agree on what a contract looks like, and duplicating those
builders here would let them drift.

Coverage is weighted towards the two things that are easy to break silently:
that the claim is untouched, and that the module does nothing at all when
disabled.

[calc]: https://github.com/openimis/openimis-be-calculation_py
[pce]: https://github.com/openimis/openimis-be-provider_contract_py