import 'package:http/http.dart' as http;

import 'fetch_result.dart';

/// Fetch a URL including the browser's session cookies.
///
/// On the web this is an XHR with withCredentials — cross-origin cookies
/// ride along only when the target site sends CORS allow-credentials
/// headers and its session cookie is SameSite=None. Used by the VM test
/// stub with a plain (credentialless) client.
Future<CredentialsFetchResult> fetchWithCredentials(String url) async {
  final client = http.Client();
  try {
    final resp = await client.get(Uri.parse(url));
    return CredentialsFetchResult(resp.statusCode, resp.body, resp.headers);
  } finally {
    client.close();
  }
}
