# Database Schema Changes

Klangk's SQLite schema evolves through **ordered, once-only migrations**:
an append-only list in
[`src/klangk/klangk/model/migrations/__init__.py`](https://github.com/mcdonc/klangk/blob/main/src/klangk/klangk/model/migrations/__init__.py),
applied automatically at startup (`init_db`) and recorded in the
`schema_migrations` table. If a row's id is recorded there, its migration
never runs again.

## Adding a migration

1. Create `src/klangk/klangk/model/migrations/m00NN_<slug>.py`
   exposing `migration = Migration(N, "00NN_<slug>", apply)`, and append
   it to the `MIGRATIONS` list in the package `__init__.py` in id order.
   Never renumber, reorder, or edit a shipped migration — append a new
   one instead.
2. `apply` receives the DB connection (the same `execute`/`commit`/
   `rollback` surface `init_db` uses). The runner wraps it in one
   `BEGIN IMMEDIATE` transaction committed together with the record row —
   do **not** issue your own `BEGIN`/`COMMIT` inside a migration.
3. Add a test in
   `src/klangk/klangkd-tests/tests/test_migrations.py`.

The historical `CREATE TABLE IF NOT EXISTS` pile in
`klangk/model/schema.py` is frozen as the pre-migration baseline; its
ad-hoc repair blocks are already-applied history and stay put.

## Failure behavior (operators)

If a migration raises, its transaction — DDL included, because SQLite DDL
is transactional only under an explicit `BEGIN` — is rolled back and the
migration is **not** recorded. Startup fails; the next boot retries the
same migration. Prior migrations stay applied and recorded. A migration
that fails repeatedly (see the server log for
`Applying schema migration <name>`) means an operator must inspect the
database; nothing half-applied is left behind in the meantime.

Renaming a shipped migration is detected and refused at startup
(`Migration names are frozen once shipped`) — a silent rename would fork
history between the recorded name and the code.
