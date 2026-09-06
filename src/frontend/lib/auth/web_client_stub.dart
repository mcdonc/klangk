/// Non-web build: this client never marks its mints (#3230) — the DPoP
/// backend has no key here, so sessions behave like the CLI's (unbound
/// by design, never deadline-limited).
const bool kWebClient = false;
