import 'dart:async';
import 'package:http/http.dart' as http;
import '../ws/ws_client.dart';

/// Handles browser_request messages from the backend bridge.
///
/// Listens to [WsClient.browserRequests] and dispatches actions.
/// Currently handles built-in actions (fetch, celebrate, beep).
/// Plugin-based dispatch will be added in a later phase.
class BrowserDelegate {
  final WsClient _client;
  final http.Client _httpClient;
  StreamSubscription<Map<String, dynamic>>? _subscription;

  BrowserDelegate(this._client, {http.Client? httpClient})
      : _httpClient = httpClient ?? http.Client(); // coverage:ignore-line

  void start() {
    _subscription = _client.browserRequests.listen(_handleRequest);
  }

  void stop() {
    _subscription?.cancel();
    _subscription = null;
  }

  Future<void> _handleRequest(Map<String, dynamic> request) async {
    final id = request['id'] as String?;
    if (id == null) return;

    final action = request['action'] as String?;
    Map<String, dynamic> result;

    try {
      switch (action) {
        case 'fetch':
          result = await _handleFetch(request);
        case 'celebrate':
          result = {'status': 'ok'};
          onCelebrate?.call();
        case 'beep':
          result = {'status': 'ok'};
          onBeep?.call();
        default:
          result = {'error': 'unknown action: $action'};
      }
    } catch (e) {
      result = {'error': 'action failed: $e'};
    }

    _client.sendBrowserResponse(id, result);
  }

  Future<Map<String, dynamic>> _handleFetch(
      Map<String, dynamic> request) async {
    final url = request['url'] as String?;
    if (url == null) {
      return {'error': 'missing url'};
    }

    final uri = Uri.tryParse(url);
    if (uri == null || !uri.hasScheme) {
      return {'error': 'invalid url: $url'};
    }

    final method = (request['method'] as String?) ?? 'GET';
    final rawHeaders = request['headers'] as Map<String, dynamic>?;
    final headers = <String, String>{};
    if (rawHeaders != null) {
      for (final entry in rawHeaders.entries) {
        if (entry.value != null) {
          headers[entry.key] = entry.value.toString();
        }
      }
    }
    final body = request['body'] as String?;

    try {
      final httpRequest = http.Request(method.toUpperCase(), uri);
      httpRequest.headers.addAll(headers);
      if (body != null) httpRequest.body = body;

      final response = await _httpClient
          .send(httpRequest)
          .then(http.Response.fromStream)
          .timeout(const Duration(seconds: 30));

      return {
        'status': response.statusCode,
        'headers': response.headers,
        'body': response.body,
      };
    } catch (e) {
      return {'error': 'fetch failed: $e'};
    }
  }

  /// Callback for celebrate action. Set by the widget tree.
  void Function()? onCelebrate;

  /// Callback for beep action. Set by the widget tree.
  void Function()? onBeep;
}
