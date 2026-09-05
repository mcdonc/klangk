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
