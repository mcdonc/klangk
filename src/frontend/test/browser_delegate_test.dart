import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/browser/browser_delegate.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

class _FakeWebSocketChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  Future<void> get ready => Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  List<dynamic> get sentMessages => _sink.sent;

  void dispose() => _incoming.close();
}

class _FakeSink extends Fake implements WebSocketSink {
  final List<dynamic> sent = [];

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

class _TestPlugin extends ToolPlugin {
  int callCount = 0;
  String lastAction = '';

  @override
  Map<String, ToolHandler> get handlers => {
        'celebrate': (request) async {
          callCount++;
          lastAction = 'celebrate';
          return 'celebrated!';
        },
        'beep': (request) async {
          callCount++;
          lastAction = 'beep';
          return 'beeped!';
        },
      };

  @override
  Map<String, StreamingToolHandler> get streamingHandlers => {
        'stream_action': (request, emit) async {
          callCount++;
          lastAction = 'stream_action';
          emit('Hel');
          emit('lo');
          return 'Hello';
        },
      };
}

class _ThrowingPlugin extends ToolPlugin {
  @override
  Map<String, ToolHandler> get handlers => {
        'throw_action': (request) async {
          throw Exception('plugin boom');
        },
      };
}

List<Map<String, dynamic>> _browserResponses(_FakeWebSocketChannel channel) {
  return channel.sentMessages
      .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
      .where((m) => m['cmd'] == 'browser_response')
      .toList();
}

List<Map<String, dynamic>> _browserChunks(_FakeWebSocketChannel channel) {
  return channel.sentMessages
      .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
      .where((m) => m['cmd'] == 'browser_chunk')
      .toList();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('BrowserDelegate', () {
    late WsClient client;
    late _FakeWebSocketChannel channel;
    late BrowserDelegate delegate;
    late MockClient mockHttp;
    late ToolPluginRegistry registry;
    late _TestPlugin testPlugin;

    setUp(() {
      client = WsClient();
      channel = _FakeWebSocketChannel();
      client.connectForTest(channel);
      mockHttp = MockClient((request) async {
        return http.Response('mock-body', 200);
      });
      registry = ToolPluginRegistry();
      testPlugin = _TestPlugin();
      registry.register(testPlugin);
      delegate =
          BrowserDelegate(client, httpClient: mockHttp, registry: registry);
      delegate.start();
    });

    tearDown(() {
      delegate.stop();
      client.disconnect();
      client.dispose();
    });

    test('dispatches celebrate to plugin', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-1',
        'action': 'celebrate',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(testPlugin.callCount, 1);
      expect(testPlugin.lastAction, 'celebrate');
      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['id'], 'req-1');
      expect(responses[0]['status'], 'ok');
      expect(responses[0]['result'], 'celebrated!');
    });

    test('streams chunks then final response for stream requests', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-s',
        'action': 'stream_action',
        'stream': true,
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final chunks = _browserChunks(channel);
      expect(chunks.map((c) => c['delta']).toList(), ['Hel', 'lo']);
      expect(chunks.every((c) => c['id'] == 'req-s'), isTrue);

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['id'], 'req-s');
      expect(responses[0]['result'], 'Hello');
    });

    test('dispatches beep to plugin', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-2',
        'action': 'beep',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(testPlugin.callCount, 1);
      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['status'], 'ok');
      expect(responses[0]['result'], 'beeped!');
    });

    test('returns error for unknown action', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-3',
        'action': 'unknown_action',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['id'], 'req-3');
      expect(responses[0]['error'], contains('Unknown action'));
    });

    test('returns error for missing action', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-null',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('missing action'));
    });

    test('ignores request without id', () async {
      channel.serverSend({
        'type': 'browser_request',
        'action': 'celebrate',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(_browserResponses(channel), isEmpty);
    });

    test('plugin exception returns error', () async {
      final throwRegistry = ToolPluginRegistry();
      throwRegistry.register(_ThrowingPlugin());
      delegate.stop();
      delegate = BrowserDelegate(client,
          httpClient: mockHttp, registry: throwRegistry);
      delegate.start();

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-boom',
        'action': 'throw_action',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('action failed'));
    });

    test('stop cancels subscription', () async {
      delegate.stop();

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-5',
        'action': 'celebrate',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(_browserResponses(channel), isEmpty);
    });

    test('clipboard_write copies text to clipboard', () async {
      // Mock the platform clipboard channel so Clipboard.setData works.
      String? clipboardText;
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          clipboardText = (call.arguments as Map)['text'] as String?;
        }
        return null;
      });

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-clip-1',
        'action': 'clipboard_write',
        'text': 'hello clipboard',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['id'], 'req-clip-1');
      expect(responses[0]['status'], 'ok');
      expect(clipboardText, 'hello clipboard');

      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null);
    });

    test('clipboard_write with missing text returns error', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-clip-empty',
        'action': 'clipboard_write',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('missing text'));
    });

    test('clipboard_write failure returns error', () async {
      // Make Clipboard.setData throw.
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          throw PlatformException(code: 'FAIL', message: 'clipboard denied');
        }
        return null;
      });

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-clip-fail',
        'action': 'clipboard_write',
        'text': 'denied text',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('clipboard write failed'));

      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null);
    });

    test('clipboard_write with empty text returns error', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-clip-blank',
        'action': 'clipboard_write',
        'text': '',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('missing text'));
    });

    test('fetch returns response from HTTP client', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-1',
        'action': 'fetch',
        'url': 'http://example.com/data',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['id'], 'req-fetch-1');
      expect(responses[0]['status'], 200);
      expect(responses[0]['body'], 'mock-body');
    });

    test('fetch with POST method', () async {
      final requests = <http.BaseRequest>[];
      final postClient = MockClient((request) async {
        requests.add(request);
        return http.Response('post-result', 201);
      });
      delegate.stop();
      delegate =
          BrowserDelegate(client, httpClient: postClient, registry: registry);
      delegate.start();

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-2',
        'action': 'fetch',
        'url': 'http://example.com/submit',
        'method': 'POST',
        'body': 'payload',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(requests.length, 1);
      expect(requests[0].method, 'POST');
      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['status'], 201);
      expect(responses[0]['body'], 'post-result');
    });

    test('fetch passes custom headers', () async {
      final requests = <http.BaseRequest>[];
      delegate.stop();
      final headerClient = MockClient((request) async {
        requests.add(request);
        return http.Response('ok', 200);
      });
      delegate =
          BrowserDelegate(client, httpClient: headerClient, registry: registry);
      delegate.start();

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-hdr',
        'action': 'fetch',
        'url': 'http://example.com/api',
        'headers': {'Authorization': 'Bearer tok123'},
      });
      await Future.delayed(const Duration(milliseconds: 50));

      expect(requests.length, 1);
      expect(requests[0].headers['Authorization'], 'Bearer tok123');
    });

    test('fetch with invalid url returns error', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-invalid',
        'action': 'fetch',
        'url': 'not-a-url',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('invalid url'));
    });

    test('fetch missing url returns error', () async {
      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-3',
        'action': 'fetch',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('missing url'));
    });

    test('fetch HTTP error returns error', () async {
      delegate.stop();
      final errorClient = MockClient((request) async {
        throw Exception('network error');
      });
      delegate =
          BrowserDelegate(client, httpClient: errorClient, registry: registry);
      delegate.start();

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-fetch-4',
        'action': 'fetch',
        'url': 'http://example.com/fail',
      });
      await Future.delayed(const Duration(milliseconds: 50));

      final responses = _browserResponses(channel);
      expect(responses.length, 1);
      expect(responses[0]['error'], contains('fetch failed'));
    });
  });

  group('WsClient browser streams', () {
    test('browser_request emitted on stream', () async {
      final client = WsClient();
      final channel = _FakeWebSocketChannel();
      client.connectForTest(channel);

      final requests = <Map<String, dynamic>>[];
      client.browserRequests.listen(requests.add);

      channel.serverSend({
        'type': 'browser_request',
        'id': 'req-1',
        'action': 'fetch',
        'url': 'http://example.com',
      });
      await Future.delayed(Duration.zero);

      expect(requests.length, 1);
      expect(requests[0]['action'], 'fetch');

      client.disconnect();
      client.dispose();
    });

    test('sendBrowserResponse sends correct JSON', () {
      final client = WsClient();
      final channel = _FakeWebSocketChannel();
      client.connectForTest(channel);

      client.sendBrowserResponse('req-1', {'status': 200, 'body': 'hello'});

      final msg = jsonDecode(channel.sentMessages.last as String)
          as Map<String, dynamic>;
      expect(msg['cmd'], 'browser_response');
      expect(msg['id'], 'req-1');
      expect(msg['status'], 200);
      expect(msg['body'], 'hello');

      client.disconnect();
      client.dispose();
    });

    test('custom events emitted on stream', () async {
      final client = WsClient();
      final channel = _FakeWebSocketChannel();
      client.connectForTest(channel);

      final events = <Map<String, dynamic>>[];
      client.customEvents.listen(events.add);

      channel.serverSend({
        'type': 'event',
        'event': {
          'type': 'CUSTOM',
          'name': 'container_stopped',
          'value': {},
        },
      });
      await Future.delayed(Duration.zero);

      expect(events.length, 1);
      expect(events[0]['event']['name'], 'container_stopped');

      client.disconnect();
      client.dispose();
    });
  });
}
