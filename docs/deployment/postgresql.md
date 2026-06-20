# PostgreSQL

By default, Klangk uses SQLite. For production multi-server deployments, PostgreSQL is supported as an alternative database backend.

## Configuration

Set `KLANGK_SQLA_DB_URL` to a PostgreSQL SQLAlchemy URL:

```bash
KLANGK_SQLA_DB_URL=postgresql+asyncpg://klangk:secret@localhost:5432/klangk
```

When `KLANGK_SQLA_DB_URL` is unset, Klangk defaults to `sqlite+aiosqlite:///{KLANGK_DATA_DIR}/klangk.db`.

## Setting up PostgreSQL

### With devenv (local development)

Uncomment the `services.postgres` block in `devenv.nix`:

```nix
services.postgres = {
  enable = true;
  listen_addresses = "127.0.0.1";
  port = 5432;
  initialDatabases = [ { name = "klangk"; } ];
};
```

Then add to your `.env`:

```bash
KLANGK_SQLA_DB_URL=postgresql+asyncpg://localhost:5432/klangk
```

### With an external PostgreSQL server

Create a database and user:

```sql
CREATE USER klangk WITH PASSWORD 'your-secret-password';
CREATE DATABASE klangk OWNER klangk;
```

Then set the connection URL in `.env`:

```bash
KLANGK_SQLA_DB_URL=postgresql+asyncpg://klangk:your-secret-password@db-host:5432/klangk
```

The `file:` prefix is supported for reading the URL from a secrets file:

```bash
KLANGK_SQLA_DB_URL=file:/run/secrets/database_url
```

## Schema management

Klangk creates all tables automatically on startup (`init_db`). No separate migration step is needed. The same schema is used for both SQLite and PostgreSQL.

## Switching from SQLite to PostgreSQL

There is no built-in migration tool for moving data between backends. For a fresh deployment, simply set `KLANGK_SQLA_DB_URL` and start Klangk -- tables will be created automatically and the default user will be seeded.

To migrate existing data, export from SQLite and import into PostgreSQL manually.

## SQLite vs PostgreSQL

| Aspect   | SQLite                     | PostgreSQL                             |
| -------- | -------------------------- | -------------------------------------- |
| Setup    | Zero-config, file-based    | Requires a running server              |
| Scaling  | Single server only         | Horizontal scaling, connection pooling |
| Backups  | Copy the `.db` file        | `pg_dump`, streaming replication       |
| Best for | Development, single-server | Production, multi-server               |
