/// Shared client-side format validators.
///
/// These mirror the server-side rules (`klangk.auth._EMAIL_RE`) and exist
/// only to give immediate inline feedback; the server stays authoritative.

final RegExp _emailRe = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');

/// Whether [value] is a syntactically valid email address —
/// `local@domain.tld`, no whitespace, single `@`.
bool isValidEmail(String value) => _emailRe.hasMatch(value.trim());
