<!-- markdownlint-disable MD013 -->

# Logging

Everything klangkd logs — its own records, uvicorn's startup/error/access logs, and the third-party libraries it runs — flows through a single root logger with a single format. `KLANGKD_LOG_FORMAT` chooses what that stream looks like, and `KLANGKD_LOG_FILE` optionally adds a file sink that is always machine-parseable, ready to forward to a SIEM or any central log platform.

## Console format: `KLANGKD_LOG_FORMAT`

- **`text`** (the default) — the human-readable colored console format.
- **`json`** — one JSON object per line: `timestamp` (ISO-8601 UTC), `level`, `logger`, `message`, plus `exc_info` (the formatted traceback) when a record carries an exception.

The JSON form contains no ANSI color codes, so the whole stream is uniform and parseable. Formatting never raises: a record with mismatched `%`-format arguments (or any other pathological input) degrades to a best-effort message instead of dumping a raw traceback line into the stream. Every line stays a parseable JSON object.

## The file sink: `KLANGKD_LOG_FILE`

Set `KLANGKD_LOG_FILE` to a path and every record (at or above `KLANGKD_LOG_LEVEL`) is **additionally** written there — always as JSON, one object per line, regardless of the console format. The typical split:

```text
console (stderr): KLANGKD_LOG_FORMAT=text   # humans watching the service
file:            always JSON                # rsyslog imfile / fluent-bit → SIEM
```

`~` is expanded, relative paths resolve against the process working directory, and the path is probed for writability at startup — an unwritable path aborts boot rather than silently dropping the log stream.

## Forwarding to a SIEM

The file sink is the integration point for the host's log shipper. With rsyslog's `imfile`:

```text
module(load="imfile")
input(type="imfile"
      File="/var/log/klangk/klangkd.jsonl"
      Tag="klangkd"
      Severity="info")
```

or with fluent-bit, a `tail` input on the same path (set `Parser` to `json`). A JSON-lines file needs no grok patterns — each line is already structured.

Under a container runtime you can skip the file entirely: with `KLANGKD_LOG_FORMAT=json`, klangkd's stdout/stderr is itself a JSON stream, and the runtime's log driver (`json-file`, journald) hands it to the host shipper unchanged.

## Built-in audit-record forwarding

The host-shipper path forwards klangkd's **log lines**. When you want the **audit records** themselves — the `audit_events` identity/privilege stream, the `container_events` lifecycle history, the `egress_consent` verdict trail — shipped by klangkd with no shipper on the host (STIG SV-222481/482: audit records to a different system / centralized log repository), configure a forwarding target (#3252):

```bash
KLANGKD_AUDIT_FORWARD_URL=https://siem.example.com/ingest   # JSON POST batches
KLANGKD_AUDIT_FORWARD_SYSLOG=tls://siem.example.com:6514    # RFC 5424 over TLS
KLANGKD_AUDIT_FORWARD_HEADER="Authorization: Splunk hec-token"  # optional auth
```

`KLANGKD_AUDIT_FORWARD_HEADER` is one optional HTTP header sent with every URL-target POST, as `Name: value` — the auth most collectors require (Splunk HEC wants `Authorization: Splunk <token>`; bearer-token collectors want `Authorization: Bearer <token>`). It applies to the URL target only; the syslog transport carries no header. The URL target sends exactly this one optional header and nothing else — a collector needing more must sit behind a small authenticating relay.

Both settings are optional and independent — with neither set the forwarder is off and nothing is read or sent. Both may be set; each target then receives every record. Both are validated at startup (a malformed value aborts boot) and reload on SIGHUP. Each target keeps its **own** delivery cursor: one target being down delays only that target (the sweep retries it with backoff while the other keeps flowing), and a target you re-point (change the URL or host) starts from zero and receives the retained backlog again — at-least-once permits the replay.

### What ships, and in what shape

Every **new row** of the three tables ships once, in insert order, per table:

- The URL target receives `POST` bodies of the form `{"records": [...]}` — one batch every few seconds, up to 500 records per batch per table. Each record carries its table's full columns (structured `detail` decoded), plus `source` (the table name) and `forward_cursor` (the ordering key: the AUTOINCREMENT row id for the two event tables, the trigger-assigned sequence number for `egress_consent`) so a receiver can order and deduplicate per source.
- The syslog target receives one RFC 5424 line per record — facility `audit`, severity `info`, the record's own event time, this host, `klangkd`, the pid, the source table as MSGID, and the full record as the JSON message. Lines are newline-framed over one TCP/TLS connection per delivery attempt (the framing most TCP receivers accept; RFC 5425 TLS receivers that demand RFC 6587 octet-counted framing need a relay in front), and each attempt is bounded by a 10-second timeout so a hung receiver cannot stall forwarding. `tls://` verifies the server certificate against the system trust store (a private CA belongs in the approved baseline, `KLANGKD_TRUSTED_CA_DIR`).

An `egress_consent` row ships when it is created — typically as a pending request; the decision applied to the row afterwards is an update to that row, not a new row, so it does not forward again. A receiver wanting decision state reads it from the klangkd database (e.g. the [backup pipeline](../reference/backup-restore.md)).

### Delivery guarantees and failure behavior

Delivery is **at-least-once**: a batch is delivered, then the per-target, per-table cursor (persisted in the klangkd database, `audit_forward_state`) advances past it. A crash between the two replays that batch, so a receiver must deduplicate (the `forward_cursor` + `source` pair identifies a record). A restart resumes right after the last accepted record. One caveat inherent to syslog over TCP without application-level acknowledgements: if the receiver resets the connection in the instant after accepting the bytes, the batch counts as delivered — the SIEM either has it or reports the loss, visible as the next sweep's retry. The cursor keys are never reused — the event tables' ids are AUTOINCREMENT and consent rows carry a trigger-assigned sequence — so a retention prune or a workspace cascade delete cannot make a new row invisible behind the cursor.

A target that is down **delays only itself** — rows stay queued in the tables themselves, and klangkd retries that target with exponential backoff (one sweep interval after the first failure, doubling per consecutive failure, capped at five minutes) while any other configured target keeps its normal sweep cadence. The failure is visible:

- `GET /audit` (public, like `/health`) reports `forwarding` with `healthy`, the per-table `pending` queue depths, `last_error` (the exception class), and the `last_success_at` / `last_failure_at` timestamps. The key appears only while a target is configured.
- klangkd's journal logs each failed sweep at `WARNING`.

Two limits to plan around: the retention sweeps (`KLANGKD_AUDIT_EVENTS_RETENTION_DAYS` etc.) keep running while delivery lags, so a target down longer than a retention window loses the oldest unforwarded rows — keep windows comfortably larger than any expected outage. And the tables' row caps bound the queue; size them for the expected outage if you rely on forwarding as the delivery path.

## Rotation

Two mechanisms, pick one per path:

**External rotation (the default).** The file sink watches its own inode and reopens when an external rotator renames the file — `logrotate`, `newsyslog`, rsyslog all work without telling klangkd anything. No in-app rotation settings are set; the platform owns rotation.

**In-app rotation.** Set a trigger and klangkd owns rotation instead:

| Setting                         | Meaning                                                                                                                                                                          |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KLANGKD_LOG_FILE_MAX_BYTES`    | Size trigger: roll the file once it reaches this many bytes. `0` (the default) = off. A file may overshoot by one record (the check runs before each write).                     |
| `KLANGKD_LOG_FILE_ROTATE`       | Time trigger: `hourly`, `daily`, `weekly`, or `monthly`, rolled at UTC boundaries (weekly = Monday). Empty (the default) = off. Both triggers can be combined — either fires.    |
| `KLANGKD_LOG_FILE_BACKUP_COUNT` | Rotated files to keep: the current file becomes `<path>.1`, existing `.N` shift up, the oldest beyond the count is deleted. Default `3`; `0` discards the rotated file outright. |

Don't mix the two: once any trigger is set, keep external rotators off the same path.

Rotation failures fail soft. If the file's path breaks at runtime (a directory replaces it, permissions change, a rename fails), the next log call does not raise — klangkd emits one warning, suspends the file sink, and keeps the console stream live. The next reload (see below) rebuilds the sink and heals it.

## Reload and validation

All of the logging settings are live on `SIGHUP` reload (see [Process Signals](../deployment/signals.md)) — format, file path, and rotation can all change without a restart. A path change closes the old sink and opens the new one. Malformed values (an unknown format or rotate interval, a negative threshold, an unwritable path) abort startup rather than half-configuring logging.

The complete field list, including defaults, is in [Environment Variables](../reference/environment.md) and [`klangkd.yaml` configuration](../reference/klangkd-config.md).
