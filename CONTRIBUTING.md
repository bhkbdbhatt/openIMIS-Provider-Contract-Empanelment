# Contributing

Thanks for considering a contribution. This module is part of the
[openIMIS](https://openimis.org/) ecosystem and follows the conventions of the
openIMIS backend assembly.

Read [AGENTS.md](AGENTS.md) before writing code. It documents the architectural
rules this module must respect; violating them causes data corruption, silent
security holes, or unmergeable migrations.

## Workflow

1. Branch off `develop` — `feature/<slug>` or `fix/<slug>`.
2. Implement inside `provider_contract/`.
3. Add or update tests in `provider_contract/tests/`, and BDD steps under
   `features/steps/` for user-visible flows.
4. Regenerate migrations if models changed (never hand-write them).
5. Run the tests and the linter.
6. Update `README.md` and this file.
7. Record a decision in the ADR table in `AGENTS.md` §10 if the change is not
   obviously reversible.
8. Open a pull request against `develop`.

## Before you open a PR

```bash
# from the openIMIS backend assembly (manage.py sits directly in openIMIS/)
cd openIMIS
OPENIMIS_CONF=../openimis-dev.json python manage.py test --keepdb --timing provider_contract

# lint, from the assembly root so the assembly .flake8 applies
cd ..
flake8 provider_contract --ignore=W503
```

Both must pass. The `flake8` job in CI runs with `--ignore W503,E501`.

## Things that will fail review

- **A hand-written or hand-edited migration.** Run `makemigrations`.
- **A partial or conditional unique constraint.** openIMIS supports MSSQL as
  well as PostgreSQL; uniqueness is enforced in services inside a transaction
  (ADR-002).
- **A model inheriting straight from `django.db.models.Model`.** Use
  `HistoryModel`, or `HistoryBusinessModel` for anything effective-dated.
- **A query without row security.** Every queryset must honour
  `settings.ROW_SECURITY` via `build_user_location_filter_query` (§4.5 of
  AGENTS.md).
- **A gate change that rejects, cancels, or edits a claim.** The gate is
  advisory (ADR-006).
- **An import from the policyholder `contract` module.** The name collision is
  a coincidence; the domains are unrelated (§1 of AGENTS.md).
- **A provider identifier typed `UUID!` in GraphQL.** `HealthFacility.uuid` is
  a `CharField`, so the argument type is `String!`.

## Commit messages

Short imperative subject, one logical change per commit. Reference the openIMIS
ticket when there is one.

## Reporting security issues

Do not open a public issue for a security vulnerability. Contact
<contact@openimis.org> and follow the openIMIS service desk process.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

By contributing you agree that your contributions are licensed under AGPL v3,
matching the rest of openIMIS.