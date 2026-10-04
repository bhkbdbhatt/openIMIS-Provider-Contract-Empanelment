"""Load the openIMIS legacy schema and run the provider_contract suite.

Everything happens in one process because pgserver shuts the server down when
the interpreter exits.

Reproduces what `ghcr.io/openimis/openimis-pgsql:develop` +
`python manage.py test --keepdb` do in CI, without Docker:

  1. start a throwaway PostgreSQL 16 (bundled in the pgserver wheel);
  2. load the legacy schema the openimis-pgsql image would have loaded into
     public -- openIMIS has many `managed = False` models pointing at legacy
     tbl* tables that Django never creates;
  3. point Django at it and run the tests.

Note there is deliberately NO `create schema django` step, even though
PSQL_DATABASE_OPTIONS pins search_path=django,public. openIMIS relies on the
`django` schema being absent so that everything resolves to `public`; creating
it splits each Django-created table (schema django) from the sequences its own
migrations add unqualified (schema public) and migrate dies at
claim.0026_add_sequences.

Two further deviations from the image:
  * json_schema_extension.sql is skipped. The `postgres-json-schema` extension
    has to be compiled, and nothing in the schema or in any installed module
    calls json_schema_is_valid (0 references).
  * TIME_ZONE is forced to GMT. This PostgreSQL build has no
    share/postgresql/timezone directory, so it rejects 'UTC' and falls back to
    GMT; aligning Django's zone with the server's makes the SET a no-op.

Override the three machine-specific paths with PCE_PGDATA, PCE_SQLDIR and
PCE_ASSEMBLY; defaults are shown below.
"""

import os
import sys
from urllib.parse import urlparse

BASE = os.environ.get("PCE_PGDATA", r"C:\Users\Asus\AppData\Local\Temp\pce_pg")
SQLDIR = os.environ.get("PCE_SQLDIR", r"C:\Users\Asus\AppData\Local\Temp\db_pgsql\database scripts")
ASSEMBLY = os.environ.get("PCE_ASSEMBLY", r"C:\Users\Asus\AppData\Local\Temp\openimis_asm\openimis-be_py")
OPENIMIS_DIR = os.path.join(ASSEMBLY, "openIMIS")
TEST_DB = "test_imis"

# Docker executes docker-entrypoint-initdb.d in lexical order; its COPY glob
# `0[2345]_*.sql` picks up only 02..05, so 01_modular_base.sql and
# 01_django.sql are deliberately absent from the image.
FILES = [
    "00_dump.sql",
    "02_aux_functions.sql",
    "03_views.sql",
    "04_functions.sql",
    "05_stored_procs.sql",
]

os.environ["LOAD_ENV"] = ""
os.environ["OPENIMIS_CONF"] = os.path.join(ASSEMBLY, "openimis-dev.json")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openIMIS.settings")
# openIMIS logs django.db.backends at DEBUG, which echoes every statement and
# buries the test output.
os.environ.setdefault("DJANGO_LOG_LEVEL", "WARNING")
# core's DEFAULT_CFG sets async_mutations from MODE: anything other than PROD
# runs mutations inline. Without this, GraphQL mutation tests queue to Celery
# and poll forever waiting for a worker that is not running.
os.environ.setdefault("MODE", "DEV")
sys.path.insert(0, OPENIMIS_DIR)
# split_settings.include() resolves relative to cwd, which breaks across drives.
os.chdir(OPENIMIS_DIR)

import pgserver  # noqa: E402

print("[1/4] starting postgres...", flush=True)
url = urlparse(pgserver.get_server(BASE).get_uri())
user, port, host = url.username, url.port, url.hostname
print(f"      {host}:{port} as {user}", flush=True)

import psycopg2  # noqa: E402


def connect(dbname):
    c = psycopg2.connect(host=host, port=port, user=user, dbname=dbname)
    c.autocommit = True
    return c


print(f"[2/4] loading legacy schema into {TEST_DB}...", flush=True)
c = connect("postgres")
with c.cursor() as cur:
    # A previous run that died without letting pgserver shut down cleanly leaves
    # connections behind, and DROP DATABASE then blocks forever waiting on them.
    cur.execute(
        "select pg_terminate_backend(pid) from pg_stat_activity "
        "where datname=%s and pid <> pg_backend_pid()",
        (TEST_DB,),
    )
    if cur.rowcount:
        print(f"      terminated {cur.rowcount} stale connection(s)")
    cur.execute("select 1 from pg_database where datname=%s", (TEST_DB,))
    if cur.fetchone():
        cur.execute(f'drop database "{TEST_DB}"')
        print(f"      dropped stale {TEST_DB}")
    cur.execute(f'create database "{TEST_DB}"')
c.close()

c = connect(TEST_DB)
# NOTE: deliberately NOT creating a `django` schema. PSQL_DATABASE_OPTIONS pins
# search_path=django,public, and openIMIS relies on the `django` schema being
# absent so that everything resolves to `public`. Creating it splits each
# Django-created table (schema django) from the sequences its own migrations
# add unqualified (schema public), which fails with "sequence must be in same
# schema as table it is linked to" at claim.0026_add_sequences.

for name in FILES:
    body = open(os.path.join(SQLDIR, name), encoding="utf-8", errors="replace").read()
    with c.cursor() as cur:
        try:
            cur.execute(body)
            print(f"      OK   {name}", flush=True)
        except Exception as exc:
            print(f"      FAIL {name}: {' '.join(str(exc).split())[:160]}", flush=True)
            with c.cursor() as cur2:
                cur2.execute("rollback")

with c.cursor() as cur:
    cur.execute("select count(*) from information_schema.tables where table_schema='public'")
    print(f"      public tables: {cur.fetchone()[0]}", flush=True)
    # camelCase, so they must stay quoted or they fold to lowercase
    for t in ("tblUsers", "tblUsersDistricts", "tblLocation", "tblServices", "tblClaim"):
        cur.execute("select to_regclass(%s)", (f'public."{t}"',))
        got = cur.fetchone()[0]
        print(f"      {t}: {got or 'MISSING'}", flush=True)
c.close()

print("[3/4] pointing django at it...", flush=True)
os.environ.update({
    "DB_DEFAULT": "postgresql",
    "DB_HOST": host,
    "DB_PORT": str(port),
    "DB_NAME": TEST_DB,
    "DB_TEST_NAME": TEST_DB,
    "DB_USER": user,
    "DB_PASSWORD": "",
    "CELERY_BROKER_URL": "memory://openIMIS-test//",
    "CELERY_RESULT_BACKEND": "cache+memory://openIMIS-test//",
})

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connections  # noqa: E402

cfg = settings.DATABASES["default"]
print(f"      {cfg['ENGINE']} name={cfg['NAME']} host={cfg['HOST']}:{cfg['PORT']} "
      f"user={cfg['USER']} test={cfg['TEST']['NAME']} options={cfg.get('OPTIONS')}",
      flush=True)

# timezone_name is a cached_property, so it has to be primed on the wrapper,
# not just in settings.
settings.TIME_ZONE = "GMT"
for alias in connections:
    connections[alias].__dict__["timezone_name"] = "GMT"
print(f"      TIME_ZONE={settings.TIME_ZONE} USE_TZ={settings.USE_TZ}", flush=True)

print("[4/4] running tests...", flush=True)
labels = sys.argv[1:] or ["provider_contract"]
try:
    call_command("test", "--keepdb", "--noinput", *labels, verbosity=2)
except SystemExit as exc:
    print(f"\nexit status: {exc.code}", flush=True)
    raise
