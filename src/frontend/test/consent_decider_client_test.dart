import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/consent/consent_decider_client.dart';
import 'package:klangk_frontend/consent/consent_request.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Minimal fake WebSocketChannel mirroring ws_client_test.dart's fake.
class _FakeChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();
  final Completer<void>? _readyCompleter;
  final bool _failReady;
  int? _closeCode;

  _FakeChannel({bool failReady = false, Completer<void>? readyCompleter})
      : _failReady = failReady,
        _readyCompleter = readyCompleter;

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  int? get closeCode => _closeCode;

  @override
  Future<void> get ready {
    if (_failReady) return Future.error('Connection refused');
    if (_readyCompleter != null) return _readyCompleter.future;
    return Future.value();
  }

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));
  void serverSendRaw(String raw) => _incoming.add(raw);

  /// Close the inbound stream, simulating a server-side disconnect. Sets the
  /// reported [closeCode] so tests can drive the auth-close path.
  void serverClose([int? code]) {
    _closeCode = code;
    _incoming.close();
  }

  /// Inject a stream error (transient decode glitch) without closing.
  void serverError(Object error) => _incoming.addError(error);

  List<dynamic> get sent => _sink.sent;

  void dispose() => _incoming.close();
}

class _FakeSink extends Fake implements WebSocketSink {
  final List<dynamic> sent = [];
  bool closeCalled = false;

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future close([int? closeCode, String? closeReason]) async {
    closeCalled = true;
  }
}

/// A channel whose sink.add always throws — exercises the verdict/ping
/// send-failure defensive catches.
class _ThrowingSinkChannel extends _FakeChannel {
  @override
  WebSocketSink get sink => _ThrowingSink();
}

class _ThrowingSink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) => throw StateError('sink closed');

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

/// Builds an AuthService whose token is already loaded (ready to connect).
Future<AuthService> _authedAuthService({String token = 'test-token'}) async {
  SharedPreferences.setMockInitialValues({'klangk_jwt': token});
  final auth = AuthService();
  await Future.delayed(Duration.zero);
  return auth;
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    ConsentDeciderClient.testChannelFactory = null;
    ConsentDeciderClient.testBackoffOverride = null;
  });

  tearDown(() {
    ConsentDeciderClient.testChannelFactory = null;
    ConsentDeciderClient.testBackoffOverride = null;
    testBaseUrlOverride = null;
  });

  ConsentDeciderClient _client(
    _FakeChannel channel, {
    required AuthService auth,
    Duration pingInterval = const Duration(minutes: 1),
    Duration countdownInterval = const Duration(seconds: 1),
  }) {
    ConsentDeciderClient.testChannelFactory = (_) => channel;
    return ConsentDeciderClient(
      workspaceId: 'ws-1',
      auth: auth,
      pingInterval: pingInterval,
      countdownInterval: countdownInterval,
    );
  }

  group('initial state', () {
    test('not connected, no pending', () {
      final auth = AuthService();
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      expect(c.connected, isFalse);
      expect(c.connecting, isFalse);
      expect(c.hasPending, isFalse);
      expect(c.pending, isEmpty);
      expect(c.paused, isNull);
      expect(c.authFailed, isFalse);
      c.dispose();
    });
  });

  group('connect', () {
    test('connects when a token is available', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      expect(c.connected, isTrue);
      expect(c.connecting, isFalse);
      c.dispose();
    });

    test('no-ops when there is no token', () async {
      final auth = AuthService(); // not logged in
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      expect(c.connected, isFalse);
      expect(channel.sent, isEmpty);
      c.dispose();
    });

    test('idempotent: second connect is a no-op', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      await c.connect();
      expect(c.connected, isTrue);
      c.dispose();
    });
  });

  group('inbound frames', () {
    test('egress_request adds a held request', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSend({
        'type': 'egress_request',
        'request': {
          'id': 'r1',
          'workspace_id': 'ws-1',
          'dest_host': 'example.com',
          'dest_port': 443,
          'process_name': 'curl',
          'requested_at': DateTime.now().millisecondsSinceEpoch / 1000.0,
        },
      });
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isTrue);
      expect(c.pending.single.id, 'r1');
      c.dispose();
    });

    test('egress_request with a bad payload is ignored', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSend({
        'type': 'egress_request',
        'request': {'workspace_id': 'ws-1'}, // missing id
      });
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isFalse);
      c.dispose();
    });

    test('egress_resolved removes the request', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSend({
        'type': 'egress_request',
        'request': {
          'id': 'r1',
          'workspace_id': 'ws-1',
          'dest_host': 'h',
          'requested_at': 0,
        },
      });
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isTrue);
      channel.serverSend({
        'type': 'egress_resolved',
        'request_id': 'r1',
        'decision': 'allowed',
      });
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isFalse);
      c.dispose();
    });

    test('egress_rules sets paused', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSend({
        'type': 'egress_rules',
        'workspace_id': 'ws-1',
        'allow_list': [],
        'allowed': [],
        'denied': [],
        'paused': null,
      });
      await Future.delayed(Duration.zero);
      expect(c.paused, isNull);
      // Forward-compat: a future server that reports paused=true is tracked.
      channel.serverSend({
        'type': 'egress_rules',
        'paused': true,
      });
      await Future.delayed(Duration.zero);
      expect(c.paused, isTrue);
      c.dispose();
    });

    test('pong and error frames do not change pending state', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSend({'type': 'pong'});
      channel.serverSend({'type': 'error', 'message': 'bad verdict'});
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isFalse);
      expect(c.connected, isTrue);
      c.dispose();
    });

    test('malformed and unknown frames are ignored', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverSendRaw('not json');
      channel.serverSendRaw('[]'); // a list, not an object
      channel.serverSend({'type': 'something_new', 'foo': 1});
      await Future.delayed(Duration.zero);
      expect(c.hasPending, isFalse);
      expect(c.connected, isTrue);
      c.dispose();
    });

    test('non-string stream events are ignored', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel._incoming.add(12345); // bytes, not string
      await Future.delayed(Duration.zero);
      expect(c.connected, isTrue);
      c.dispose();
    });
  });

  group('reconnect resets stale state', () {
    test('pending rows clear before the new snapshot arrives', () async {
      final auth = await _authedAuthService();
      final channels = <_FakeChannel>[];
      ConsentDeciderClient.testChannelFactory = (_) {
        final ch = _FakeChannel();
        channels.add(ch);
        return ch;
      };
      ConsentDeciderClient.testBackoffOverride = (_) => Duration.zero;
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      await c.connect();
      channels.first.serverSend({
        'type': 'egress_request',
        'request': {
          'id': 'stale',
          'workspace_id': 'ws-1',
          'dest_host': 'h',
          'requested_at': 0,
        },
      });
      await Future.delayed(Duration.zero);
      expect(c.pending.any((r) => r.id == 'stale'), isTrue);

      // Drop then reconnect (fresh channel). The reconnect clears pending
      // before the new snapshot arrives.
      channels.first.serverClose();
      await Future.delayed(const Duration(milliseconds: 30));
      expect(c.pending.any((r) => r.id == 'stale'), isFalse);
      c.dispose();
    });
  });

  group('verdicts', () {
    test('allow sends an allow/once verdict frame', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      c.allow('r1');
      expect(channel.sent, hasLength(1));
      final msg = jsonDecode(channel.sent.single as String) as Map;
      expect(msg['type'], 'verdict');
      expect(msg['request_id'], 'r1');
      expect(msg['decision'], 'allowed');
      expect(msg['scope'], 'once');
      c.dispose();
    });

    test('deny sends a deny/once verdict frame', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      c.deny('r9');
      final msg = jsonDecode(channel.sent.single as String) as Map;
      expect(msg['decision'], 'denied');
      expect(msg['scope'], 'once');
      c.dispose();
    });

    test('verdict is a no-op when not connected', () async {
      final auth = AuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      c.allow('r1'); // never connected
      expect(channel.sent, isEmpty);
      c.dispose();
    });
  });

  group('liveness ping', () {
    test('sends ping frames at the ping interval', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(
        channel,
        auth: auth,
        pingInterval: const Duration(milliseconds: 20),
      );
      await c.connect();
      // Wait for >=2 ping ticks.
      await Future.delayed(const Duration(milliseconds: 70));
      final pings = channel.sent
          .map((s) => jsonDecode(s as String))
          .where((m) => m['type'] == 'ping')
          .toList();
      expect(pings.length, greaterThanOrEqualTo(2));
      c.dispose();
    });
  });

  group('reconnect', () {
    test('reconnects with backoff after a transient drop', () async {
      final auth = await _authedAuthService();
      final channels = <_FakeChannel>[];
      ConsentDeciderClient.testChannelFactory = (_) {
        final ch = _FakeChannel();
        channels.add(ch);
        return ch;
      };
      ConsentDeciderClient.testBackoffOverride = (_) => Duration.zero;
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      await c.connect();
      expect(channels, hasLength(1));
      expect(c.connected, isTrue);
      channels.first.serverClose(); // transient close (no 4001/4002)
      await Future.delayed(const Duration(milliseconds: 30));
      // A fresh channel was created for the reconnect and we connected again.
      expect(channels.length, greaterThanOrEqualTo(2));
      expect(c.connected, isTrue);
      c.dispose();
    });

    test('does NOT reconnect after an auth-failure close', () async {
      final auth = await _authedAuthService();
      final channels = <_FakeChannel>[];
      ConsentDeciderClient.testChannelFactory = (_) {
        final ch = _FakeChannel();
        channels.add(ch);
        return ch;
      };
      ConsentDeciderClient.testBackoffOverride = (_) => Duration.zero;
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      await c.connect();
      expect(c.authFailed, isFalse);
      channels.first.serverClose(4001); // auth failure
      await Future.delayed(const Duration(milliseconds: 30));
      expect(c.authFailed, isTrue);
      expect(c.connected, isFalse);
      expect(channels, hasLength(1)); // no second connect attempt
      c.dispose();
    });

    test('reconnects to a healthy server after initial connect failures',
        () async {
      final auth = await _authedAuthService();
      var failsRemaining = 2;
      ConsentDeciderClient.testChannelFactory = (_) {
        if (failsRemaining > 0) {
          failsRemaining--;
          return _FakeChannel(failReady: true);
        }
        return _FakeChannel();
      };
      ConsentDeciderClient.testBackoffOverride = (_) => Duration.zero;
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      await c.connect();
      expect(c.connected, isFalse);
      // The failed connects reschedule via backoff; once the factory returns a
      // healthy channel the client connects.
      await Future.delayed(const Duration(milliseconds: 40));
      expect(c.connected, isTrue);
      c.dispose();
    });
  });

  group('remaining countdown', () {
    test('clamps at zero for an expired hold', () {
      final auth = AuthService();
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      final req = parseConsentRequest({
        'id': 'old',
        'workspace_id': 'ws-1',
        'dest_host': 'h',
        'requested_at': 0,
      })!;
      expect(c.remaining(req), 0.0);
      c.dispose();
    });

    test('is positive for a fresh hold', () {
      final auth = AuthService();
      final c = ConsentDeciderClient(workspaceId: 'ws-1', auth: auth);
      final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
      final req = parseConsentRequest({
        'id': 'fresh',
        'workspace_id': 'ws-1',
        'dest_host': 'h',
        'requested_at': now,
      })!;
      expect(c.remaining(req), greaterThan(100.0));
      c.dispose();
    });
  });

  group('countdown tick', () {
    test('fires notifyListeners while pending and self-cancels when empty',
        () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(
        channel,
        auth: auth,
        countdownInterval: const Duration(milliseconds: 20),
      );
      await c.connect();
      var notifications = 0;
      c.addListener(() => notifications++);
      channel.serverSend({
        'type': 'egress_request',
        'request': {
          'id': 'r1',
          'workspace_id': 'ws-1',
          'dest_host': 'h',
          'requested_at': 0,
        },
      });
      await Future.delayed(Duration.zero);
      // The 20ms countdown tick should fire at least once while pending.
      await Future.delayed(const Duration(milliseconds: 60));
      expect(notifications, greaterThan(0));
      // Clearing pending makes the next tick self-cancel.
      channel.serverSend({'type': 'egress_resolved', 'request_id': 'r1'});
      await Future.delayed(const Duration(milliseconds: 60));
      c.dispose();
    });
  });

  group('defensive send failures', () {
    test('verdict send failure is swallowed (no throw)', () async {
      final auth = await _authedAuthService();
      final channel = _ThrowingSinkChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      // Must not throw.
      c.allow('r1');
      c.deny('r2');
      c.dispose();
    });

    test('ping send failure is swallowed (no throw)', () async {
      final auth = await _authedAuthService();
      final channel = _ThrowingSinkChannel();
      final c = _client(
        channel,
        auth: auth,
        pingInterval: const Duration(milliseconds: 20),
      );
      await c.connect();
      await Future.delayed(const Duration(milliseconds: 60));
      c.dispose();
    });
  });

  group('stream errors', () {
    test('stream onError is handled without disconnecting', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(channel, auth: auth);
      await c.connect();
      channel.serverError(Exception('transient decode glitch'));
      await Future.delayed(Duration.zero);
      // Still connected (onError doesn't close the stream).
      expect(c.connected, isTrue);
      c.dispose();
    });
  });

  group('real backoff (no override)', () {
    test('uses the constructor reconnectDelays', () async {
      final auth = await _authedAuthService();
      final channels = <_FakeChannel>[];
      ConsentDeciderClient.testChannelFactory = (_) {
        final ch = _FakeChannel();
        channels.add(ch);
        return ch;
      };
      // No testBackoffOverride: exercises the real _backoffDelay path.
      // reconnectDelays=[0] -> Duration.zero backoff -> fast reconnect.
      final c = ConsentDeciderClient(
        workspaceId: 'ws-1',
        auth: auth,
        reconnectDelays: const [0.0],
      );
      await c.connect();
      expect(channels, hasLength(1));
      channels.first.serverClose();
      await Future.delayed(const Duration(milliseconds: 40));
      expect(channels.length, greaterThanOrEqualTo(2));
      expect(c.connected, isTrue);
      c.dispose();
    });
  });

  group('dispose', () {
    test('closes the channel and stops timers', () async {
      final auth = await _authedAuthService();
      final channel = _FakeChannel();
      final c = _client(
        channel,
        auth: auth,
        pingInterval: const Duration(milliseconds: 20),
      );
      await c.connect();
      c.dispose();
      expect(channel._sink.closeCalled, isTrue);
      expect(c.connected, isFalse);
    });
  });
}
