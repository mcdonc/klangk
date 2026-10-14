/// Result of one credentialed fetch: the HTTP status, the body text and
/// the (lowercased-key) response headers.
class CredentialsFetchResult {
  final int status;
  final String body;
  final Map<String, String> headers;

  const CredentialsFetchResult(this.status, this.body, this.headers);

  /// Any 2xx counts as authenticated: the stock probe (oauth2-proxy's
  /// /oauth2/auth) answers 202 on success, not 200.
  bool get is2xx => status >= 200 && status < 300;
}

/// The auth-probe URL convention this feature polls: same origin as the
/// target URL, path /oauth2/auth — the oauth2-proxy auth endpoint every
/// canonical nginx auth_request integration already proxies. It answers
/// plain 401 while unauthenticated and 2xx once the session cookie is
/// accepted (statuses a background fetch can read).
String? defaultAuthProbe(String url) {
  final uri = Uri.tryParse(url);
  if (uri == null || !uri.hasScheme || uri.host.isEmpty) return null;
  final port = uri.hasPort ? ':${uri.port}' : '';
  return '${uri.scheme}://${uri.host}$port/oauth2/auth';
}
