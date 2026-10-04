# Provider Contract & Empanelment — Agent Guide

Backend reference module for [openIMIS](https://openimis.org/): provider
empanelment workflows, contract lifecycle management, per-contract fee
schedules, and the claims gate that ties contracts to claim adjudication.

## 1. Purpose and scope

This module owns the lifecycle of the relationship between a **provider** and a
**health insurance scheme**: whether the provider is empanelled, on what terms,
for which services, at what price, and whether a given claim is inside that
relationship.

In scope:

- Multi-stage empanelment (application → document verification → site visit →
  committee review → decision), configurable per scheme
- Contract lifecycle: draft → negotiation → signed → active → renewal →
  renewal/termination/expiry, with full amendment versioning
- Per-contract, effective-dated fee schedules materialized into openIMIS
  pricelists
- A read-only claims gate (`check_provider_empanelment`, `applicable_fee`) and
  the `ClaimScopeViolation` queue it produces

**Out of scope — do not implement here:**

- The policyholder `contract` module (`openimis-be-contract_py`). Despite the
  name collision, `tblContract` / `tblContractDetails` /
  `tblContractContributionPlanDetails` are *policyholder* agreements tied to
  contribution plans. Nothing in this module may import from it or from
  `policyholder`, `contribution_plan`, or `payment`.
- Beneficiary data. That is the `individual` / `insuree` / `policy` domain.
- Individual practitioners (HCPs). openIMIS has no practitioner entity; v1
  empanels **facilities** only, via `location.HealthFacility`.
- Capitation/PMPM and DRG pricing. v1 ships fixed-fee-per-service and bundled
  only. See §10.

## 2. Module layout

```
provider_contract/
├── apps.py           AppConfig + DEFAULT_CFG (permissions, behaviour flags)
├── models.py         All ORM entities
├── urls.py           Empty urlpatterns (openIMIS is GraphQL-first)
├── signals.py        bind_service_signals() — the only sanctioned hook point
├── services/         Business logic; GraphQL mutations call into here
│   ├── common.py           Configuration access + the dated lookups
│   ├── contract_lifecycle.py  Contract state machine, amendment versioning,
│   │                         and the service-enforced uniqueness rules
│   ├── empanelment.py      Workflow execution + document checklist
│   ├── fees.py             Fee schedules, fee rows, pricelist materialization
│   └── violations.py       Read-only claims gate + its queue
├── gql/              GraphQL layer
│   ├── queries.py       GQLTypes, gate verdict types, and `Query`
│   └── mutations.py     `OpenIMISMutation` subclasses wrapping the services
├── schema.py          Assembly entry point: re-exports `Query`, `Mutation`,
│                      `bind_signals` (the assembly imports `<app>.schema`)
├── tests/            Django tests (run via the assembly test runner)
├── migrations/       GENERATED — never hand-written
└── locale/           gettext translations
```

Repository root also carries `features/` (BDD), `AGENTS.md` (this file, which
holds the architecture rules and the ADR table) and `README.md`.

## 3. Getting started

```bash
git clone https://github.com/openimis/openimis-be-provider_contract_py.git
cd openimis-be-provider_contract_py
pip install -e .
```

Register it in the assembly. `openimis-be_py/openimis.json` (CI/production):

```json
{
  "name": "provider_contract",
  "pip": "git+https://github.com/openimis/openimis-be-provider_contract_py.git@develop#egg=openimis-be-provider-contract"
}
```

and `openimis-dev.json` (local dev, editable):

```json
{ "name": "provider_contract", "pip": "-e file:/path/to/backend-packages/provider_contract" }
```

Then from the assembly:

```bash
cd openIMIS
OPENIMIS_CONF=../openimis-dev.json python manage.py migrate
OPENIMIS_CONF=../openimis-dev.json python manage.py runserver
```

Use **Python 3.11**.

## 4. Architecture rules (non-negotiable)

These exist because violating them produces data corruption, silent security
holes, or unmergeable migrations.

### 4.1 Base models

| Need | Base class |
|---|---|
| Ordinary record, mostly immutable once written | `core.models.HistoryModel` |
| Anything with an effective period or version chain | `core.models.HistoryBusinessModel` (= `HistoryModel` + `ValidityMixin`) |

Never inherit from `django.db.models.Model` directly. The bases supply UUID
primary keys, `django-simple-history`, `dirtyfields`, the caching manager, and
the audit columns that the rest of openIMIS relies on.

### 4.2 Saving requires a user

`HistoryModel.user_created` and `user_updated` are **non-nullable**. Always
pass the user:

```python
contract.save(user=user)          # correct
ProviderContract.objects.create() # IntegrityError
```

`version` is incremented by `HistoryModel.save()` whenever dirty fields are
detected, and a save with no dirty fields returns `None` without writing.

> **`get_or_create()` is not a substitute for `save(user=...)`.**
> `HistoryModel.save()` resolves the actor through `get_user(user, username)`,
> which — when given neither — falls back to
> `User.objects.filter(i_user_id=1).first()` and then *overwrites*
> `user_created` with whatever it found. So passing `user_created=` to
> `objects.create()` is ignored, and `get_or_create()` cannot forward a `user`
> at all: rows end up attributed to an arbitrary user, or the insert violates
> NOT NULL. Where you need `get_or_create` semantics, do the lookup yourself
> and call `save(user=...)` — see `_get_or_create_as_user()` in
> `management/commands/seed_empanelment_catalogue.py`.

### 4.3 Soft delete only

`HistoryModel.delete()` sets `is_deleted=True`. Never hard-delete:

```python
instance.delete()                                   # correct
instance.__class__.objects.filter(pk=pk).delete()   # WRONG
```

Query through `filter_queryset()` / `filter_validity()` so soft-deleted and
out-of-validity rows are excluded.

### 4.4 Effective dating and versioning

Use `ValidityMixin`'s `date_valid_from` / `date_valid_to` / `replacement_uuid`
and its `replace_object(data, **{"username": ...})` helper. Amendment is
*never* an in-place update of a dated row — close the old row and open a new
one. This is what makes historical claim pricing reproducible.

> **Two validity vocabularies coexist.** Models this module points *at*
> (`location.HealthFacility`, `medical_pricelist.*`, `claim.*`) inherit
> `core.models.VersionedModel` and use `validity_from` / `validity_to` with an
> `AutoField` PK. Models this module *owns* use `date_valid_from` /
> `date_valid_to` with a UUID PK. Do not conflate them in filters, GraphQL
> output, or service code. A join that filters one model with the other's
> column name is a silent bug.

> **`version` is not `version_no`.** `version` is inherited from
> `HistoryModel` and is an optimistic-concurrency counter bumped on every
> dirty save. The contract's business version number is `version_no`
> (`IntegerField`). Both appear on `ProviderContract`. Never let them shadow
> each other.

> **`contract_number` is deliberately NOT unique.** Amendment and renewal both
> call `replace_object`, which copies the row and keeps every field — including
> `contract_number`. A `unique=True` there makes the *second version of any
> contract* fail to insert with `duplicate key ... ProviderContract_ContractNo_key`.
> Only the "one open version per `contract_number`" rule is enforced, in
> `ProviderContractService._assert_single_open_version()`. See ADR-002 and
> migration `0004_alter_..._contract_number_and_more`.

> **`replace_object()` returns `None`, and the version link is a forward
> reference.** It writes the *successor's* id onto the row it superseded:
> `old.replacement_uuid == new.id`, and `new.replacement_uuid is None`. So the
> successor has to be read back as
> `objects.get(uuid=contract.replacement_uuid)` after a `refresh_from_db()`.
> Querying `objects.get(replacement_uuid=contract.id)` inverts the link and
> raises `DoesNotExist`.

> **`filter_validity()` is inherited but broken for owned models.** The static
> `filter_validity` on `OpenIMISHistoryMixin` filters on `active` and
> `date_deactivated`, which exist on `OpenIMISModel` — not on `HistoryModel`,
> which uses `is_deleted`. Any `HistoryBusinessModel` in this module must
> define its own classmethod:
>
> ```python
> @classmethod
> def filter_validity(cls, queryset=None, date=None, **kwargs):
>     date = date or datetime.now()
>     if queryset is None:
>         queryset = cls.objects.all()
>     return queryset.filter(
>         Q(date_valid_from__lte=date) & (Q(date_valid_to__isnull=True) | Q(date_valid_to__gt=date))
>     )
> ```

> **`date_valid_from` is not nullable.** `ValidityMixin` declares
> `default=datetime.now` with no `null=True`, so every row must have one. See
> `replace_object()`, which copies a record and stamps `now` on the successor.

### 4.5 Row security is mandatory

openIMIS enforces district-level data isolation at the queryset layer. A query
that omits it leaks contracts and fee schedules across districts. Follow the
`claim.Claim.get_queryset` pattern — note the `filter_queryset()` call first, so
soft-deleted rows never reach the caller:

```python
@classmethod
def get_queryset(cls, queryset, user):
    queryset = cls.filter_queryset(queryset)
    # GraphQL calls with an info object while Rest calls with the user itself
    if isinstance(user, ResolveInfo):
        user = user.context.user
    if settings.ROW_SECURITY and user.is_anonymous:
        return queryset.filter(id=-1)
    if settings.ROW_SECURITY:
        queryset = LocationManager().build_user_location_filter_query(
            user._u, queryset=queryset, prefix="provider__location", loc_types=["D"],
        )
    return queryset
```

**`prefix` must be the FK path all the way to `location.Location`, not to the
provider.** `build_user_location_filter_query` builds
`Q(f"{prefix}__in", allowed_locations)`, so a prefix of `provider__` would
compare health facilities against location ids. The correct prefixes are:

| Model | `prefix` |
|---|---|
| `ProviderContract`, `EmpanelmentProcess`, `ContractFeeBulkOperation` | `provider__location` |
| `ContractFeeSchedule` | `contract__provider__location` |
| `ContractFeeItem` | `fee_schedule__contract__provider__location` |
| `ContractBenefitPackage`, `ContractServiceCategory` | `contract__provider__location` |
| `EmpanelmentStageTransition` | `process__provider__location` |
| `EmpanelmentProcessDocument` | `process__provider__location` |
| `ClaimScopeViolation` | `provider__location` |

Import `LocationManager` from `location.models` at module scope — `claim` and
`location` both do — and never call `build_user_location_filter_query` with a
`user` that is not an `InteractiveUser`; it logs a warning and returns the
**unfiltered** queryset.

> **Resolve the actor, and fail closed.** `build_user_location_filter_query`
> checks `isinstance(user, InteractiveUser)` and, for anything else, logs
> `Access without filter` and returns the queryset **unfiltered**. Two
> consequences, both of which were live bugs before the tests caught them:
>
> * `claim.Claim.get_queryset` — the pattern quoted above — passes
>   `user._u`. That does **not** help: `User._u` is
>   `self.i_user or self.officer or self.claim_admin or self.t_user`, and only
>   `i_user` is an `InteractiveUser`. That call therefore either does nothing or
>   returns an unfiltered queryset, depending on how the account was
>   provisioned.
> * `user.is_anonymous` is **not** safe to read unconditionally.
>   `InteractiveUser` is not a Django auth user and has no such attribute, so
>   probing it before normalising raises `AttributeError`.
>
> `ProviderContractQueryMixin` therefore resolves the actor explicitly and
> empties the queryset when it cannot:
>
> ```python
> is_anonymous = (
>     not isinstance(user, core_models.InteractiveUser)
>     and getattr(user, "is_anonymous", False)
> )
> if settings.ROW_SECURITY and is_anonymous:
>     return queryset.filter(id=EMPTY_UUID)
> if settings.ROW_SECURITY:
>     filter_user = cls._interactive_user_for(user)   # InteractiveUser | None
>     if filter_user is None:
>         return queryset.filter(id=EMPTY_UUID)        # fail closed
>     ...
> ```
>
> `_interactive_user_for` resolves a `core.User` through its `i_user`
> relation and returns `None` otherwise. Returning the queryset unchanged for
> an unrecognised actor is the one thing this must never do.
>
> `provider_contract/tests/test_row_security.py` asserts that a district-scoped
> caller genuinely cannot read another district's contracts, and that passing a
> bare `core.User` still filters rather than silently disabling security.
>
> **A district must have a parent to be granted at all.**
> `UserDistrict.get_user_districts` filters on
> `location__parent__isnull=False`. A district built with a bare
> `create_test_location("D")` therefore never reaches the caller's allowed
> list, and every row-security assertion passes *vacuously* — an empty result
> is indistinguishable from correct isolation. Use
> `tests/base.py::create_district_with_parent`, which creates the region too.

### 4.6 Identifiers and money

- Provider and pricelist `uuid` columns are `CharField(max_length=36)`, **not**
  `UUIDField`. GraphQL arguments for provider identifiers are therefore
  `String!`, never `UUID!`.
- **`uuid` on our own models is a Python property, not a column.**
  `HistoryModel` defines `id = UUIDField(primary_key=True)` plus
  `uuid` as a property aliasing `id`. Declaring a `uuid` field on a subclass
  shadows the property and breaks `HistoryModelManager`, which annotates
  `uuid=F("id")` so callers can filter `uuid=`. Never declare `uuid` on a model
  in this module.
- Money is `DecimalField(max_digits=18, decimal_places=2)` to match
  `medical_pricelist.ServicesPricelistDetail.price_overrule`. Using more decimal
  places loses precision when fee rows are projected into a pricelist.

### 4.7 Mutation log tables

Every entity exposed through a GraphQL mutation needs a companion row-log model
following the openIMIS convention:

```python
class ProviderContractMutation(UUIDModel, ObjectMutation):
    contract = models.ForeignKey(ProviderContract, models.DO_NOTHING, related_name="mutations")
    mutation = models.ForeignKey(MutationLog, models.DO_NOTHING, related_name="provider_contracts")

    class Meta:
        managed = True
        db_table = "provider_contract_ProviderContractMutation"
```

Use `core.models.UUIDModel` and `core.models.ObjectMutation`; use the
`@register_service_signal`-decorated create mutation helper where available.

### 4.8 Portable DDL — PostgreSQL **and** MSSQL

openIMIS supports both engines. Anything Postgres-only will break MSSQL
deployments:

- No partial/conditional unique constraints. In particular the "one ACTIVE
  contract per facility" rule and the "one open version per `contract_number`"
  rule are **enforced in services**, inside `transaction.atomic()` with
  `select_for_update()` on the provider and contract rows. This is weaker than
  a DB constraint and that trade-off is deliberate (ADR-002).
- No `JSONField` `db_check` with a JSON-schema. The Postgres
  `json_schema` extension is not available on MSSQL. Validate JSON payloads in
  services instead.
- `ForeignKey(..., on_delete=models.DO_NOTHING)` is the openIMIS default.

### 4.9 Configuration

Every tunable goes in `DEFAULT_CFG` in `apps.py` with a safe default, so the
module runs unconfigured. Read it via the `AppConfig` class attributes
(`ProviderContractConfig.pce_gate_enabled`), never by re-reading the database
inside a request. Configuration must never contain credentials —
`ModuleConfiguration.is_exposed` gates what the API may return.

### 4.10 Table naming

New tables are `<module>_<ModelName>`: `provider_contract_EmpanelmentProcess`,
`provider_contract_ProviderContractMutation`. The `tbl*` prefix is reserved
for tables reverse-engineered from the legacy `.mssql` schema — do not use it.

## 5. GraphQL conventions

The module exposes `<app>.schema`, which the assembly discovers by importing it
and collecting `Query`, `Mutation` and `bind_signals`. Types live in
`provider_contract/gql/`.

- Queries are `snake_case`, plural for collections (`provider_contracts`),
  singular for single fetch (`provider_contract`). Full-text search helpers
  are suffixed `_str` (`provider_contracts_str`).
- Mutations are `create_<entity>` / `update_<entity>` / `delete_<entity>` plus
  domain verbs (`amend_provider_contract`, `decide_empanelment_process`).
- Every mutation emits the default openIMIS signals so `report` and
  `api_etl` stay wired up.
- The gate queries are **read paths**: they never mutate, never raise on a
  negative result, and always return a verdict object with a machine-readable
  `reasonCode`.

> **A mutation returns `None` on success — nothing else.** `async_mutate`'s
> return value is read as `error_messages`, and the base class does
> `if not error_messages: mark_as_successful() else: mark_as_failed(...)`. A
> *dict* of useful results is truthy, so returning one marks the mutation
> **failed** and files your payload under `MutationLog.error`. `None` is the
> only success signal; clients read the rows back over the query API.

> **Failures are reported on the `MutationLog`, not the response.** The base
> class replies with an `internalId` and then does the work, and it wraps
> `async_mutate` in a bare `except Exception`. Returning an error list and
> raising therefore produce the *same* observable outcome: log status `ERROR`.
> A test must assert on the log (`assert_mutation_error`), never on
> `"errors" in response`, which will be false either way.

> **Input types extend `OpenIMISMutation.Input`, not `InputObjectType`.**
> Graphene builds the final input as `type(name, (InputObjectType, input_class),
> ...)`, so a top-level input that already subclasses `InputObjectType` makes
> the bases inconsistent and the class body raises `TypeError: Cannot create a
> consistent method resolution order (MRO) for bases InputObjectType, Input`.
> Nested inputs (a row inside a bulk update) are ordinary `InputObjectType`.

> **`filter_fields` may not contain `uuid`.** On this module's models `uuid` is
> a *property* aliasing the UUID `id`, not a column (see 4.6), so graphene
> rejects it: `'Meta.fields' must not contain non-model field names: uuid`.
> Filter on `id` instead. The `uuid=` *lookup* still works at query time because
> `HistoryModelManager` annotates `uuid=F("id")` — the column and the lookup are
> genuinely different things.

> **A `Query` resolver cannot rely on `self`.** graphene-django's test client
> executes with `root_value=None`, and graphene passes that root as the first
> positional argument, so a `self`-referencing resolver sees `None`. Keep
> helpers as module-level functions and ignore the root parameter.

> **Query permissions belong in `get_queryset`, not in a resolver.** A
> connection field has no resolver to hang them on, and an unauthorised caller
> must not be able to page through a collection either. Raise
> `PermissionDenied` there. Related-field resolvers (`resolve_fee_schedules`,
> …) still check, since those bypass `get_queryset`.

## 6. Integration rules

The claims gate is the module's most consequential behaviour. It is
**advisory, never punitive**.

- Never reject, cancel, or delete a claim. On a failed gate check, record a
  `ClaimScopeViolation` and leave the claim exactly where it was.
- Only `severity="BLOCKING"` may route a claim into the claim module's Review
  stage (`select_claims_for_review` / `save_claims_review`), and only when
  `pce_gate_mode == "route_to_review"`. The default is `flag_only`.
- `pce_gate_enabled = False` must make the module fully inert.
- The only sanctioned integration points are `provider_contract/signals.py`
  (`bind_service_signals`) and, for pricing, the
  `calcrule_provider_contract_scope` calculation rule. Do not monkey-patch the
  claim module, and do not open a cross-module FK from this module into
  `claim` except on `ClaimScopeViolation` (kept in its own migration batch for
  exactly that reason — see §8).

### Fee resolution modes

`pce_fee_resolution_mode` selects how a contract's fee rows reach a claim:

| Mode | Mechanism | Ceiling |
|---|---|---|
| `FACILITY_PRICELIST` (default) | On activation, materialize the effective fee rows into a dedicated `ServicesPricelist` and assign `HealthFacility.services_pricelist` | Exactly one ACTIVE contract per facility |
| `CONTRACT_CALCULE` | Leave `HealthFacility.services_pricelist` untouched; the calcrule resolves `ContractFeeItem` per (provider, service, date) | Unlimited concurrent contracts |

`applicable_fee` returns `resolutionMode` so adjudicators can see which path
priced a claim. Switching modes on a populated database requires re-running fee
materialization — document it in the PR.

## 7. Testing

Tests run through the **Django test runner inside the assembly**, not standalone:

```bash
cd openIMIS
OPENIMIS_CONF=../openimis-dev.json python manage.py test --keepdb --timing provider_contract
```

Use the provided harnesses from `openimis-be-core_py`:

- `core.models.openimis_graphql_test_case.OpenIMISGraphQLTestCase` for GraphQL
- `core.rights_role_test_case` for permission coverage — every mutation in
  `DEFAULT_CFG` needs a rights test
- `core.test_helpers` for fixture builders

Coverage expectations: every state transition in the contract lifecycle, both
fee resolution modes, and **every negative branch of the gate** (not empanelled,
out of scope, expired, no fee match) asserting that the claim was *not*
modified. A gate test that does not assert the claim is untouched is incomplete.

BDD scenarios live in `features/provider_contract.feature`.

## 8. Migration rules

> Per the assembly guide: **migrations are generated, never written by hand.**

```bash
cd openIMIS
OPENIMIS_CONF=../openimis-dev.json python manage.py makemigrations provider_contract
```

Never edit a generated migration by hand. Use `RunPython` in a separate,
forward-only data migration for backfills — never reverse a data migration
that created contracts, because contracts become financially significant once
claims have been priced against them.

Migrations are split into two batches on purpose:

1. **Batch 1** — empanelment, contract and fee entities. Self-contained; no
   foreign key into `claim`. Installable without the claim module.
2. **Batch 2** — `ClaimScopeViolation`, which holds FKs to `claim.Claim` and
   `claim.ClaimService`. Keeping it separate means a deployment that only wants
   contract governance does not inherit a hard dependency on the claim schema.

`0003_backfill_contracts_from_health_facility` then backfills `ProviderContract`
rows from the legacy `location.HealthFacility.contract_start_date` /
`contract_end_date` columns (ADR-003).

### Seeding belongs in a management command, not a migration

The default empanelment workflow, its stages and the document checklist are
created by `provider_contract/management/commands/seed_empanelment_catalogue.py`,
**not** by a data migration.

The reason is the audit columns: `HistoryModel.user_created` and `user_updated`
are non-nullable FKs to `core.User`, and `apps.get_model()` in a migration
returns a *plain* historical model with none of `HistoryModel`'s behaviour — no
`set_pk()`, no `version` bumping, and no `save(user=...)`. A seeding migration
would have to invent a UUID for the primary key and an actor to attribute the
row to, and on a fresh install there is no user to attribute it to yet even
though the catalogue is exactly what that install needs first. openIMIS core
made the same call: `core.utils.insert_role_right_for_system` is stubbed out
with `pass` and the comment *"do not manage the role and right via migrations"*.

Two more consequences worth internalising:

- `core.User` is a `TechnicalUser`/`AbstractBaseUser`. It has **no** `uuid`,
  **no** `date_created` and **no** `validity_from` column — only `id`,
  `username`, `is_superuser` and relations. Order by `username`, never by a
  timestamp. (This was a real bug caught during bring-up.)
- Because the historical model is plain, any `RunPython` that creates rows must
  pass `id=uuid.uuid4()` explicitly, or the insert fails the `NOT NULL`
  constraint on `UUID`.
- And because it is plain, it has **no nested `TextChoices` classes and no
  methods**. `apps.get_model("provider_contract", "ProviderContract").Status.EXPIRED`
  raises `AttributeError` during a real `migrate`, not just in a test.
  Migration 0003 spells the status literals out instead, and
  `test_backfill_migration` exercises the function through
  `ProjectState.from_apps(...)` precisely so this class of bug cannot come back.

## 9. Pull request checklist

1. Branch off `develop`: `feature/<slug>` or `fix/<slug>`
2. Implement in `provider_contract/`
3. Add or update tests in `provider_contract/tests/`; add BDD steps under
   `features/steps/` for user-visible flows
4. Regenerate migrations if models changed
5. `python manage.py test --keepdb --timing provider_contract`
6. `flake8 provider_contract --ignore=W503` (from the assembly root directory,
   so the assembly `.flake8` applies)
7. Update `README.md` and this file — the module repo owns its documentation
8. Record a decision in the ADR table in §10 if the change is not obviously
   reversible
9. Bump `version` in `setup.py` for a release (single quotes — the publish
   workflow rewrites it with a `sed` that only matches `version='...'`)
10. Open the PR against `openimis/openimis-be-provider_contract_py`

## 10. Roadmap

| Version | Scope |
|---|---|
| 0.1.0 (v1) | Empanelment workflow, contract lifecycle with amendment versioning, fixed-fee + bundled fee schedules, claims gate (flag-only) |
| 0.2.0 | DRG and capitation/PMPM pricing, provider scorecards and KPI alerts, de-empanelment with grace period, bulk operations and CSV import |
| 0.3.0 | Provider self-service portal, individual practitioner (HCP) credentialing |

ADRs:

| ADR | Decision |
|---|---|
| `001-fee-schedule-materialization.md` | Project contract fees into openIMIS pricelists rather than build a parallel pricing engine |
| `002-portable-ddl.md` | Enforce uniqueness in services, not with partial indexes (Postgres + MSSQL) |
| `003-health-facility-contract-columns.md` | Supersede the legacy `HealthFacility.contract_*` columns |
| `004-config-driven-fee-resolution.md` | `FACILITY_PRICELIST` vs `CONTRACT_CALCULE` |
| `005-no-scheme-id.md` | The gate API has no `schemeId`; scoping is per-instance via benefit package |
| `006-claims-gate-is-advisory.md` | The gate flags, it never rejects |

## 11. Reference

- Assembly agent guide: `openimis-be_py/AGENTS.md`
- Reference implementations: `openimis-be-core_py` (GraphQL, services,
  permissions, models), `openimis-be-contract_py` (stateful document
  lifecycle with amend/renew/terminate), `openimis-be-claim_py` (validation
  signals and review stages)
- Developer documentation: https://openimis.atlassian.net/wiki/
