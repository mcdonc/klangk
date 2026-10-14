/// Result of one credentialed fetch: the HTTP status, the body text and
/// the (lowercased-key) response headers.
class CredentialsFetchResult {
  final int status;
  final String body;
  final Map<String, String> headers;

  const CredentialsFetchResult(this.status, this.body, this.headers);
}

/// The auth-probe URL convention this feature polls: same origin as the
/// target URL, path /auth-fetch (served by the companion nginx role as a
/// plain-401-until-authenticated endpoint).
String? defaultAuthProbe(String url) {
  final uri = Uri.tryParse(url);
  if (uri == null || !uri.hasScheme || uri.host.isEmpty) return null;
  return uri.replace(path: '/auth-fetch', query: '', fragment: '').toString();
}
