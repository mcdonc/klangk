import 'package:http/browser_client.dart' as http;

import 'fetch_result.dart';

/// Fetch a URL including the browser's session cookies (XHR with
/// withCredentials). Cross-origin requests carry cookies only when the
/// target site answers CORS with Access-Control-Allow-Credentials and an
/// exact Origin, and its session cookie is SameSite=None.
Future<CredentialsFetchResult> fetchWithCredentials(String url) async {
  final client = http.BrowserClient()..withCredentials = true;
  try {
    final resp = await client.get(Uri.parse(url));
    return CredentialsFetchResult(resp.statusCode, resp.body, resp.headers);
  } finally {
    client.close();
  }
}
