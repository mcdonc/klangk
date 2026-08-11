import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/consent_decider_service.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Minimal fake WebSocketChannel mirroring ws_client_test's helper.
class _FakeChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sinkImpl = _FakeSink();
  int? _closeCode;

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sinkImpl;

  @override
  int? get closeCode => _closeCode;

  @override
  Future<void> get ready => Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  void serverClose([int? code]) {
    _closeCode = code;
    _incoming.close();
  }

  List<dynamic> get sent => _sinkImpl.sent;
}

class _FakeSink extends Fake implements WebSocketSink {
  final List<dynamic> sent = [];

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future close([int? code, String? reason]) async {}
}

/// A channel whose [sink] throws on add, to exercise sendVerdict's catch
/// path (a socket that died between the connected check and the send).
class _ThrowingChannel extends Fake implements WebSocketChannel {
  final _ctrl = StreamController<dynamic>.broadcast();

  @override
  Stream<dynamic> get stream => _ctrl.stream;

  @override
  WebSocketSink get sink => _ThrowingSink();

  @override
  int? get closeCode => null;

  @override
  Future<void> get ready => Future.value();
}

class _ThrowingSink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) => throw Exception('socket gone');

  @override
  Future close([int? code, String? reason]) async {}
}

Map<String, dynamic> _request(
        {String id = 'r1',
        String host = 'example.com',
        int port = 443,
        String? proc = 'curl',
        double at = 1000.0}) =>
    {
      'id': id,
      'workspace_id': 'ws-1',
      'dest_host': host,
      'dest_port': port,
      'process_name': proc,
      'requested_at': at,
    };

void main() {
  group('applyFrame', () {
    test('egress_request adds the request', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
          pending,
          jsonEncode({
            'type': 'egress_request',
            'request': _request(),
          }));
      expect(res.outcome, ConsentFrameOutcome.added);
      expect(res.request!.id, 'r1');
      expect(pending, contains('r1'));
      expect(pending['r1']!.destHost, 'example.com');
      expect(pending['r1']!.destPort, 443);
    });

    test('egress_resolved removes the request', () {
      final pending = <String, PendingRequest>{
        'r1': PendingRequest(id: 'r1', destHost: 'h', requestedAt: 0)
      };
      final res = ConsentDeciderService.applyFrame(
          pending, jsonEncode({'type': 'egress_resolved', 'request_id': 'r1'}));
      expect(res.outcome, ConsentFrameOutcome.resolved);
      expect(res.resolvedId, 'r1');
      expect(pending, isNot(contains('r1')));
    });

    test('pong is non-mutating', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
          pending, jsonEncode({'type': 'pong'}));
      expect(res.outcome, ConsentFrameOutcome.pong);
      expect(pending, isEmpty);
    });

    test('error surfaces the message', () {
      final res = ConsentDeciderService.applyFrame(
          {}, jsonEncode({'type': 'error', 'message': 'bad verdict'}));
      expect(res.outcome, ConsentFrameOutcome.error);
      expect(res.message, 'bad verdict');
    });

    test('malformed JSON is ignored', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(pending, 'not json{');
      expect(res.outcome, ConsentFrameOutcome.ignored);
      expect(pending, isEmpty);
    });

    test('unknown type is ignored', () {
      final res = ConsentDeciderService.applyFrame(
          {}, jsonEncode({'type': 'egress_rules', 'rules': []}));
      expect(res.outcome, ConsentFrameOutcome.ignored);
    });

    test('egress_request with a bad request payload is ignored', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
          pending, jsonEncode({'type': 'egress_request', 'request': {}}));
      expect(res.outcome, ConsentFrameOutcome.ignored);
      expect(pending, isEmpty);
    });
  });

  group('buildVerdict', () {
    test('produces a well-formed verdict frame', () {
      final raw = ConsentDeciderService.buildVerdict('r1', 'allowed', '5m');
      final msg = jsonDecode(raw) as Map<String, dynamic>;
      expect(msg, {
        'type': 'verdict',
        'request_id': 'r1',
        'decision': 'allowed',
        'scope': 'once',
        'duration': '5m',
      });
    });
  });

  group('PendingRequest.fromJson', () {
    test('parses a full payload', () {
      final r = PendingRequest.fromJson(_request(proc: 'wget'))!;
      expect(r.id, 'r1');
      expect(r.destHost, 'example.com');
      expect(r.destPort, 443);
      expect(r.processName, 'wget');
      expect(r.requestedAt, 1000.0);
    });

    test('missing dest_port yields null port', () {
      final r = PendingRequest.fromJson({
        'id': 'r2',
        'workspace_id': 'ws',
        'dest_host': 'h',
      })!;
      expect(r.destPort, isNull);
    });

    test('missing id yields null', () {
      expect(PendingRequest.fromJson({'workspace_id': 'ws'}), isNull);
    });

    test('non-map yields null', () {
      expect(PendingRequest.fromJson('nope'), isNull);
      expect(PendingRequest.fromJson(null), isNull);
    });
  });

  group('ConsentDeciderService', () {
    late _FakeChannel channel;

    setUp(() {
      channel = _FakeChannel();
      ConsentDeciderService.testChannelFactory = (_) => channel;
    });

    tearDown(() {
      ConsentDeciderService.testChannelFactory = null;
    });

    test('empty pending + not auth-failed initially', () {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      expect(svc.pending, isEmpty);
      expect(svc.authFailed, isFalse);
      svc.dispose();
    });

    test('connect + snapshot populates pending + sends verdict on the socket',
        () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.connect();
      expect(svc.connected, isTrue);
      // Server's connect snapshot. (Broadcast streams deliver on a microtask,
      // so flush before asserting.)
      channel.serverSend(
          {'type': 'egress_request', 'request': _request(host: 'a.io')});
      channel.serverSend({
        'type': 'egress_request',
        'request': _request(id: 'r2', host: 'b.io', at: 999)
      });
      await Future.delayed(Duration.zero);
      expect(
          svc.pending.map((r) => r.destHost), ['b.io', 'a.io']); // oldest-first

      // A live resolve removes it.
      channel.serverSend({'type': 'egress_resolved', 'request_id': 'r1'});
      await Future.delayed(Duration.zero);
      expect(svc.pending.map((r) => r.id), ['r2']);

      // Verdict goes out on the socket.
      svc.sendVerdict('r2', 'denied', 'once');
      expect(channel.sent, isNotEmpty);
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out['type'], 'verdict');
      expect(out['request_id'], 'r2');
      expect(out['decision'], 'denied');
      svc.dispose();
    });

    test('sendVerdict flashes (not silent) when disconnected', () {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      // Never connect -> sendVerdict flashes "disconnected" (mirrors the TUI)
      // and drops the frame; the server auto-denies on the hold timeout.
      svc.sendVerdict('r1', 'allowed', 'once');
      expect(channel.sent, isEmpty);
      expect(svc.flashMessage, contains('disconnected'));
      svc.dispose();
    });

    test('a server error frame surfaces a flash', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.connect();
      channel.serverSend({'type': 'error', 'message': 'verdict rejected'});
      await Future.delayed(Duration.zero);
      expect(svc.flashMessage, 'verdict rejected');
      svc.dispose();
    });

    test('a server error frame with no message flashes a fallback', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.connect();
      channel.serverSend({'type': 'error'});
      await Future.delayed(Duration.zero);
      expect(svc.flashMessage, 'server error');
      svc.dispose();
    });

    test('flash clears once the ttl elapses (clock-based)', () async {
      var now = DateTime.fromMillisecondsSinceEpoch(1000 * 1000, isUtc: true);
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        clock: () => now,
      );
      svc.connect();
      channel.serverSend({'type': 'error', 'message': 'boom'});
      await Future.delayed(Duration.zero);
      expect(svc.flashMessage, 'boom');
      now = now.add(const Duration(seconds: 6)); // past the 5s ttl
      expect(svc.flashMessage, isNull);
      svc.dispose();
    });

    test('sendVerdict flashes when the socket send throws', () {
      ConsentDeciderService.testChannelFactory = (_) => _ThrowingChannel();
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.connect();
      svc.sendVerdict('r1', 'allowed', 'once'); // sink.add throws
      expect(svc.flashMessage, contains('verdict send failed'));
      svc.dispose();
    });

    test('auth-fail close (4001) sets authFailed and stops reconnecting',
        () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.connect();
      channel.serverSend({'type': 'egress_request', 'request': _request()});
      await Future.delayed(Duration.zero);
      expect(svc.pending, isNotEmpty);
      channel.serverClose(4001);
      await Future.delayed(Duration.zero);
      expect(svc.authFailed, isTrue);
      svc.dispose();
    });

    test('remainingSeconds counts down from requested_at + holdTimeout', () {
      // Fixed clock at epoch-second 1000; requested at 1000, hold 120s.
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        holdTimeout: const Duration(seconds: 120),
        clock: () =>
            DateTime.fromMillisecondsSinceEpoch(1000 * 1000, isUtc: true),
      );
      final req = PendingRequest(id: 'r', destHost: 'h', requestedAt: 1000.0);
      expect(svc.remainingSeconds(req), 120);
      svc.dispose();
    });
  });
}
