/// Shared client-side format validators.
///
/// These mirror the server-side rules (`klangk.auth._EMAIL_RE`,
/// `klangk.model.users.HANDLE_RE`) and exist only to give immediate inline
/// feedback; the server stays authoritative.

/// `local@domain.tld` — no whitespace, a single `@`, a dot in the domain.
final RegExp emailRe = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');

/// Handle charset: lowercase letters, digits, dots, hyphens, underscores.
final RegExp handleRe = RegExp(r'^[a-z0-9._-]+$');

/// Whether [value] is a syntactically valid email address.
bool isValidEmail(String value) => emailRe.hasMatch(value.trim());

/// Whether [value] uses only handle characters. Call sites layer their own
/// policy on top (length cap, lowercase, reserved names) — this predicate
/// covers just the charset.
bool isValidHandleChars(String value) => handleRe.hasMatch(value.trim());
