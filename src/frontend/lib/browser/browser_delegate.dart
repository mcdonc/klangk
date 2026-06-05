import 'dart:async';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../ws/ws_client.dart';

/// Handles browser_request messages from the backend bridge.
///
/// Built-in actions (fetch) are handled directly. All other actions
/// are dispatched to the [ToolPluginRegistry] which holds handlers
/// registered by Klangk plugins.
class BrowserDelegate {
  final WsClient _client;
  final http.Client _httpClient;
  final ToolPluginRegistry _registry;
  StreamSubscription<Map<String, dynamic>>? _subscription;

  BrowserDelegate(
    this._client, {
    http.Client? httpClient,
    ToolPluginRegistry? registry,
  })  : _httpClient = httpClient ?? http.Client(), // coverage:ignore-line
        _registry = registry ?? ToolPluginRegistry(); // coverage:ignore-line

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
      if (action == 'fetch') {
        result = await _handleFetch(request);
      } else if (action != null) {
        // Streaming bridge: when the backend marks the request as a stream,
        // relay incremental deltas as browser_chunk messages; the final
        // browser_response (below) terminates the stream.
        final onChunk = request['stream'] == true
            ? (String delta) => _client.sendBrowserChunk(id, delta)
            : null;
        final response =
            await _registry.dispatch(action, request, onChunk: onChunk);
        if (response.startsWith('Unknown action:')) {
          result = {'error': response};
        } else {
          result = {'status': 'ok', 'result': response};
        }
      } else {
        result = {'error': 'missing action'};
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
}
