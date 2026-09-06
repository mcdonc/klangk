# Admin Management

Members of the `admins` group have access to the Admin page, which
provides user and group management, invitation handling, and server
scheduling for the entire Klangk instance.

## Users

[![Admin users panel](../assets/admin/users.png)](../assets/admin/users.png)

The Users tab lists all registered accounts. From here you can:

- **Create users** — add a new user with an email and password.
  Admin-created users are verified immediately (no email confirmation
  needed).
- **Edit users** — change a user's email, password, or handle.
- **Delete users** — remove a user and all their data. Each workspace
  is archived to a `.tar.gz` (the export/import format, re-importable
  via `klangk import`) before its data directory is removed. You cannot
  delete your own account.

## Groups

[![Admin groups panel](../assets/admin/groups.png)](../assets/admin/groups.png)

The Groups tab lets you organize users into named groups. Groups are
used for sharing workspaces and controlling access via
[ACL rules](authorization.md).

- **Create groups** — give the group a name and optional description.
- **Manage members** — add or remove users from a group.
- **Delete groups** — removing a group also removes any ACL entries
  that reference it.

The `admins` group is created automatically on first startup and
grants access to this Admin page. The default user is added to it
automatically.

## Server

The Server tab schedules planned maintenance for the `klangkd` process
(see [Server Scheduling](server-scheduling.md) for what stop and
recycle actually do):

- **Schedule an action** — pick **Stop** or **Recycle** and either an
  absolute time (date and time pickers, your local time) or a delay
  (`2h`, `90m`, `45s`, `2h 30m`; a bare number means minutes). The
  form shows when the action will fire.
- **Pending list** — schedules soonest-first with the same live
  countdown every connected client sees; the list follows the live
  server snapshot, so changes made elsewhere appear immediately.
- **Cancel a schedule** — each row has a cancel button with a confirm
  step; cancelling clears the countdown clients see.

## Notifications

Klangk can notify designated recipients (a System Administrator and
Information System Security Officer — SA/ISSO — in STIG terms) when
security-relevant events happen, in real time rather than only in the
audit log:

- **Account lifecycle** — a user is created (by an admin, by
  self-registration, by invitation, or automatically on a first SSO
  login), updated, deleted, unlocked, disabled, or re-enabled. Disables
  triggered automatically by the inactivity sweep notify too, as one
  message per sweep.
- **Credential and identity changes** — password, email, and handle
  changes, including self-service ones, and group membership changes.
- **Audit failures** — a failed audit-trail write alerts immediately;
  an unwritable audit table must not fail silently.
- **Capacity refusals** — a workspace start refused because host memory
  cannot fit the workspace's memory limit.

Two delivery channels are available, and both can be on at once:

- **Email** — `KLANGKD_ADMIN_NOTIFICATION_EMAILS` holds a
  comma-separated recipient list. Messages go out through the same
  SMTP or sendmail transport as the verification and invitation emails
  ([Email settings](../reference/environment.md)).
- **Webhook** — `KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL` receives one
  JSON POST per notification with the event name, timestamp, actor,
  target, detail, and source IP. Delivery is one attempt with a short
  timeout; there are no retries.

With neither channel configured, notifications are off — this is the
default. `KLANGKD_ADMIN_NOTIFY_EVENTS` narrows the allowlist of event
types that notify (the default is all of them); an unknown event name
in that list aborts startup so a typo cannot silently disable a
notification. A config-file `admin_notify_events: []` turns event
notifications off while leaving the channels configured — the
deliberate off switch (blanking the environment variable instead
restores the default allowlist). Persistent conditions (`audit.failure`, `resource.low`)
notify at most once per event name every 5 minutes, so a degraded
audit table or a full host produces one alert rather than a flood.

Notification delivery is best-effort: a failed email or webhook call
is logged as a warning and never fails or delays the action that
triggered it.
