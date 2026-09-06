import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/dpop.dart';
import 'package:klangk_frontend/workspace/consent_decider_service.dart';

import 'dpop_test_helpers.dart';
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

Map<String, dynamic> _request({
  String id = 'r1',
  String host = 'example.com',
  int port = 443,
  String? proc = 'curl',
  double at = 1000.0,
}) =>
    {
      'id': id,
      'workspace_id': 'ws-1',
      'dest_host': host,
      'dest_port': port,
      'process_name': proc,
      'requested_at': at,
    };

Map<String, dynamic> _ruleJson({
  String id = 'v1',
  String host = 'a.io',
  int port = 443,
  String? proc = 'curl',
  String decision = 'allowed',
  String? duration = '5m',
  double? decidedAt = 1000.0,
  String? decidedBy = 'alice',
}) =>
    {
      'id': id,
      'dest_host': host,
      'dest_port': port,
      if (proc != null) 'process_name': proc,
      'decision': decision,
      if (duration != null) 'duration': duration,
      if (decidedAt != null) 'decided_at': decidedAt,
      if (decidedBy != null) 'decided_by': decidedBy,
    };

Map<String, dynamic> _rulesFrame({
  String workspaceId = 'ws-1',
  List<String> allowList = const [],
  List<String> rejectList = const [],
  List<Map<String, dynamic>> allowed = const [],
  List<Map<String, dynamic>> denied = const [],
  Object? paused,
}) =>
    {
      'type': 'egress_rules',
      'workspace_id': workspaceId,
      'allow_list': allowList,
      'reject_list': rejectList,
      'allowed': allowed,
      'denied': denied,
      if (paused != null) 'paused': paused,
    };

void main() {
  group('applyFrame', () {
    test('egress_request adds the request', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
        pending,
        jsonEncode({'type': 'egress_request', 'request': _request()}),
      );
      expect(res.outcome, ConsentFrameOutcome.added);
      expect(res.request!.id, 'r1');
      expect(pending, contains('r1'));
      expect(pending['r1']!.destHost, 'example.com');
      expect(pending['r1']!.destPort, 443);
    });

    test('egress_resolved removes the request', () {
      final pending = <String, PendingRequest>{
        'r1': PendingRequest(id: 'r1', destHost: 'h', requestedAt: 0),
      };
      final res = ConsentDeciderService.applyFrame(
        pending,
        jsonEncode({'type': 'egress_resolved', 'request_id': 'r1'}),
      );
      expect(res.outcome, ConsentFrameOutcome.resolved);
      expect(res.resolvedId, 'r1');
      expect(pending, isNot(contains('r1')));
    });

    test('pong is non-mutating', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
        pending,
        jsonEncode({'type': 'pong'}),
      );
      expect(res.outcome, ConsentFrameOutcome.pong);
      expect(pending, isEmpty);
    });

    test('error surfaces the message', () {
      final res = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'error', 'message': 'bad verdict'}),
      );
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
        {},
        jsonEncode({'type': 'totally_unknown', 'x': 1}),
      );
      expect(res.outcome, ConsentFrameOutcome.ignored);
    });

    test('egress_request with a bad request payload is ignored', () {
      final pending = <String, PendingRequest>{};
      final res = ConsentDeciderService.applyFrame(
        pending,
        jsonEncode({'type': 'egress_request', 'request': {}}),
      );
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

  group('buildRevoke', () {
    test('produces a well-formed revoke frame', () {
      final msg = jsonDecode(ConsentDeciderService.buildRevoke('v1'))
          as Map<String, dynamic>;
      expect(msg, {'type': 'revoke', 'request_id': 'v1'});
    });
  });

  group('buildPause / buildUnpause', () {
    test('produces well-formed pause/unpause frames', () {
      final pause = jsonDecode(ConsentDeciderService.buildPause('15m'))
          as Map<String, dynamic>;
      expect(pause, {'type': 'pause', 'duration': '15m'});
      final unpause = jsonDecode(ConsentDeciderService.buildUnpause())
          as Map<String, dynamic>;
      expect(unpause, {'type': 'unpause'});
    });
  });

  group('ConsentRule.fromJson', () {
    test('parses a full payload', () {
      final r = ConsentRule.fromJson(_ruleJson())!;
      expect(r.id, 'v1');
      expect(r.destHost, 'a.io');
      expect(r.destPort, 443);
      expect(r.processName, 'curl');
      expect(r.decision, 'allowed');
      expect(r.duration, '5m');
      expect(r.decidedAt, 1000.0);
      expect(r.decidedBy, 'alice');
    });

    test('coerces a numeric (double) dest_port to int', () {
      final r = ConsentRule.fromJson({..._ruleJson(), 'dest_port': 443.0})!;
      expect(r.destPort, 443);
    });

    test(
        'missing dest_port -> null; non-string duration -> null; '
        'non-num decidedAt -> null', () {
      final r = ConsentRule.fromJson({
        'id': 'x',
        'dest_host': 'h',
        'decision': 'allowed',
        'duration': 5,
        'decided_at': 'nope',
      })!;
      expect(r.destPort, isNull);
      expect(r.duration, isNull);
      expect(r.decidedAt, isNull);
    });

    test('non-map / null -> null', () {
      expect(ConsentRule.fromJson('nope'), isNull);
      expect(ConsentRule.fromJson(null), isNull);
    });
  });

  group('ConsentRule equality', () {
    final a = ConsentRule(
      id: 'v1',
      destHost: 'h',
      destPort: 443,
      processName: 'curl',
      decision: 'allowed',
      duration: '5m',
      decidedAt: 1.0,
      decidedBy: 'alice',
    );

    test('equal on all fields with matching hashCode', () {
      final b = ConsentRule(
        id: 'v1',
        destHost: 'h',
        destPort: 443,
        processName: 'curl',
        decision: 'allowed',
        duration: '5m',
        decidedAt: 1.0,
        decidedBy: 'alice',
      );
      expect(a, equals(b));
      expect(a.hashCode, b.hashCode);
    });

    test('a differing field breaks equality', () {
      expect(
        a,
        isNot(
          equals(ConsentRule(id: 'v2', destHost: 'h', decision: 'allowed')),
        ),
      );
      expect(
        a,
        isNot(
          equals(ConsentRule(id: 'v1', destHost: 'h2', decision: 'allowed')),
        ),
      );
      expect(a, isNot(equals('not a rule')));
    });
  });

  group('EgressRules.fromJson', () {
    test('returns null without a workspace_id', () {
      expect(
        EgressRules.fromJson({'type': 'egress_rules', 'allow_list': []}),
        isNull,
      );
    });

    test('non-list allow_list/allowed/denied degrade to empty', () {
      final r = EgressRules.fromJson({
        'workspace_id': 'w',
        'allow_list': 'oops',
        'allowed': 'oops',
        'denied': 3,
      })!;
      expect(r.workspaceId, 'w');
      expect(r.allowList, isEmpty);
      expect(r.allowed, isEmpty);
      expect(r.denied, isEmpty);
    });

    test('skips rows that fail to parse', () {
      final r = EgressRules.fromJson({
        'workspace_id': 'w',
        'allow_list': ['a.io'],
        'allowed': [
          {'id': 'ok', 'dest_host': 'h', 'decision': 'allowed'},
          'badrow',
          5,
        ],
      })!;
      expect(r.allowList, ['a.io']);
      expect(r.allowed, hasLength(1));
      expect(r.allowed.single.id, 'ok');
    });

    test('sorts decided newest-first; null decided_at last', () {
      final r = EgressRules.fromJson({
        'workspace_id': 'w',
        'allowed': [
          {
            'id': 'old',
            'dest_host': 'h',
            'decision': 'allowed',
            'decided_at': 10.0,
          },
          {
            'id': 'new',
            'dest_host': 'h',
            'decision': 'allowed',
            'decided_at': 90.0,
          },
          {'id': 'unknown', 'dest_host': 'h', 'decision': 'allowed'},
        ],
      })!;
      expect(r.allowed.map((e) => e.id), ['new', 'old', 'unknown']);
    });

    test('stable: same decided_at keeps frame order; nulls keep order last',
        () {
      // List.sort isn't stable; ties (same decided_at, or both null) must keep
      // the frame's relative order (matches the TUI's stable sorted()).
      final r = EgressRules.fromJson({
        'workspace_id': 'w',
        'allowed': [
          {
            'id': 'first',
            'dest_host': 'h',
            'decision': 'allowed',
            'decided_at': 50.0,
          },
          {
            'id': 'second',
            'dest_host': 'h',
            'decision': 'allowed',
            'decided_at': 50.0,
          },
          {'id': 'n1', 'dest_host': 'h', 'decision': 'allowed'},
          {'id': 'n2', 'dest_host': 'h', 'decision': 'allowed'},
        ],
      })!;
      expect(r.allowed.map((e) => e.id), ['first', 'second', 'n1', 'n2']);
    });
  });

  group('EgressRules paused parsing', () {
    test('absent / not paused -> null', () {
      expect(EgressRules.fromJson({'workspace_id': 'w'})!.paused, isNull);
      expect(
        EgressRules.fromJson({
          'workspace_id': 'w',
          'paused': {'paused': false},
        })!
            .paused,
        isNull,
      );
    });

    test(
      'paused true with until -> EgressPause(until); without -> null until',
      () {
        expect(
          EgressRules.fromJson({
            'workspace_id': 'w',
            'paused': {'paused': true, 'until': 99.0},
          })!
              .paused!
              .until,
          99.0,
        );
        expect(
          EgressRules.fromJson({
            'workspace_id': 'w',
            'paused': {'paused': true},
          })!
              .paused!
              .until,
          isNull,
        );
      },
    );
  });

  group('EgressPause + EgressRules equality', () {
    test('EgressPause equality', () {
      expect(
        const EgressPause(until: 1.0),
        equals(const EgressPause(until: 1.0)),
      );
      expect(
        const EgressPause(until: 1.0).hashCode,
        const EgressPause(until: 1.0).hashCode,
      );
      expect(
        const EgressPause(until: 1.0),
        isNot(equals(const EgressPause(until: 2.0))),
      );
      expect(const EgressPause(until: 1.0), isNot(equals(const EgressPause())));
    });

    test('EgressRules equality by content', () {
      final a = EgressRules(
        workspaceId: 'w',
        allowList: ['a'],
        allowed: [ConsentRule(id: '1', destHost: 'h', decision: 'allowed')],
        denied: const [],
        paused: const EgressPause(until: 1.0),
      );
      final b = EgressRules(
        workspaceId: 'w',
        allowList: ['a'],
        allowed: [ConsentRule(id: '1', destHost: 'h', decision: 'allowed')],
        denied: const [],
        paused: const EgressPause(until: 1.0),
      );
      expect(a, equals(b));
      expect(a.hashCode, b.hashCode);
      expect(
        a,
        isNot(
          equals(
            EgressRules(
              workspaceId: 'w2',
              allowList: ['a'],
              allowed: const [],
              denied: const [],
            ),
          ),
        ),
      );
      expect(
        a,
        isNot(
          equals(
            EgressRules(
              workspaceId: 'w',
              allowList: ['b'],
              allowed: const [],
              denied: const [],
            ),
          ),
        ),
      );
      // #2503: the reject list participates in equality.
      expect(
        a,
        isNot(
          equals(
            EgressRules(
              workspaceId: 'w',
              allowList: ['a'],
              rejectList: ['bad.io'],
              allowed: const [],
              denied: const [],
            ),
          ),
        ),
      );
    });
  });

  group('applyFrame egress_rules + revoke_ack', () {
    test('egress_rules -> rules outcome with the snapshot', () {
      final res = ConsentDeciderService.applyFrame(
        {},
        jsonEncode(_rulesFrame(allowList: ['a.io'], rejectList: ['bad.io'])),
      );
      expect(res.outcome, ConsentFrameOutcome.rules);
      expect(res.rules!.allowList, ['a.io']);
      // #2503: the static reject list rides every snapshot frame (#2370);
      // a missing/malformed field degrades to empty, not a dropped frame.
      expect(res.rules!.rejectList, ['bad.io']);
      final noReject = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'egress_rules', 'workspace_id': 'w'}),
      );
      expect(noReject.rules!.rejectList, isEmpty);
    });

    test('egress_rules without workspace_id -> ignored', () {
      final res = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'egress_rules', 'allow_list': []}),
      );
      expect(res.outcome, ConsentFrameOutcome.ignored);
    });

    test('revoke_ack ok true/false and id coercion', () {
      final ok = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'revoke_ack', 'request_id': 'v1', 'ok': true}),
      );
      expect(ok.outcome, ConsentFrameOutcome.revokeAck);
      expect(ok.revokeAckId, 'v1');
      expect(ok.revokeOk, isTrue);
      final bad = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'revoke_ack', 'request_id': 5}),
      );
      expect(bad.outcome, ConsentFrameOutcome.revokeAck);
      expect(bad.revokeAckId, isNull);
      expect(bad.revokeOk, isFalse);
    });

    test('pause_ack ok true/false', () {
      final ok = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'pause_ack', 'ok': true, 'until': 1300.0}),
      );
      expect(ok.outcome, ConsentFrameOutcome.pauseAck);
      expect(ok.pauseOk, isTrue);
      final bad = ConsentDeciderService.applyFrame(
        {},
        jsonEncode({'type': 'pause_ack', 'ok': false, 'until': null}),
      );
      expect(bad.outcome, ConsentFrameOutcome.pauseAck);
      expect(bad.pauseOk, isFalse);
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

  group('PendingRequest equality', () {
    final a = PendingRequest(
      id: 'r1',
      destHost: 'h',
      destPort: 443,
      processName: 'curl',
      requestedAt: 1.0,
    );

    test('equal on all fields, with matching hashCode', () {
      final b = PendingRequest(
        id: 'r1',
        destHost: 'h',
        destPort: 443,
        processName: 'curl',
        requestedAt: 1.0,
      );
      expect(a, equals(b));
      expect(a.hashCode, b.hashCode);
      expect(a, equals(a)); // identical
    });

    test('any differing field breaks equality', () {
      expect(
        a,
        isNot(
          equals(
            PendingRequest(
              id: 'r1',
              destHost: 'h',
              destPort: 443,
              processName: 'curl',
              requestedAt: 2.0,
            ),
          ),
        ),
      ); // requestedAt differs
      expect(
        a,
        isNot(
          equals(PendingRequest(id: 'r2', destHost: 'h', requestedAt: 1.0)),
        ),
      ); // id differs
      expect(a, isNot(equals('not a request'))); // wrong type
    });
  });

  group('ConsentDeciderService', () {
    late _FakeChannel channel;

    setUp(() {
      channel = _FakeChannel();
      ConsentDeciderService.testChannelFactory = (_, __) => channel;
    });

    tearDown(() {
      ConsentDeciderService.testChannelFactory = null;
    });

    test('empty pending + not auth-failed initially', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      expect(svc.pending, isEmpty);
      expect(svc.authFailed, isFalse);
      svc.dispose();
    });

    test(
      'connect + snapshot populates pending + sends verdict on the socket',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        expect(svc.connected, isTrue);
        // Server's connect snapshot. (Broadcast streams deliver on a microtask,
        // so flush before asserting.)
        channel.serverSend({
          'type': 'egress_request',
          'request': _request(host: 'a.io'),
        });
        channel.serverSend({
          'type': 'egress_request',
          'request': _request(id: 'r2', host: 'b.io', at: 999),
        });
        await Future.delayed(Duration.zero);
        expect(svc.pending.map((r) => r.destHost), [
          'b.io',
          'a.io',
        ]); // oldest-first

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
      },
    );

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
      await svc.connect();
      channel.serverSend({'type': 'error', 'message': 'verdict rejected'});
      await Future.delayed(Duration.zero);
      expect(svc.flashMessage, 'verdict rejected');
      svc.dispose();
    });

    test('a server error frame with no message flashes a fallback', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
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
      await svc.connect();
      channel.serverSend({'type': 'error', 'message': 'boom'});
      await Future.delayed(Duration.zero);
      expect(svc.flashMessage, 'boom');
      now = now.add(const Duration(seconds: 6)); // past the 5s ttl
      expect(svc.flashMessage, isNull);
      svc.dispose();
    });

    test('sendVerdict flashes when the socket send throws', () async {
      ConsentDeciderService.testChannelFactory = (_, __) => _ThrowingChannel();
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendVerdict('r1', 'allowed', 'once'); // sink.add throws
      expect(svc.flashMessage, contains('verdict send failed'));
      svc.dispose();
    });

    test(
      'auth-fail close (4001) sets authFailed and stops reconnecting',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        channel.serverSend({'type': 'egress_request', 'request': _request()});
        await Future.delayed(Duration.zero);
        expect(svc.pending, isNotEmpty);
        channel.serverClose(4001);
        await Future.delayed(Duration.zero);
        expect(svc.authFailed, isTrue);
        svc.dispose();
      },
    );

    test(
      'must-change gate close (4004, #3172) sets authFailed and stops reconnecting',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        channel.serverSend({'type': 'egress_request', 'request': _request()});
        await Future.delayed(Duration.zero);
        expect(svc.pending, isNotEmpty);
        channel.serverClose(4004);
        await Future.delayed(Duration.zero);
        expect(svc.authFailed, isTrue);
        svc.dispose();
      },
    );

    test('remainingSeconds counts down from requested_at + holdTimeout',
        () async {
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

    test(
      'pong and unknown frames are no-ops through the live socket',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        channel.serverSend({'type': 'egress_request', 'request': _request()});
        await Future.delayed(Duration.zero);
        expect(svc.pending, hasLength(1));
        // A pong and an unknown frame neither mutate pending nor throw.
        channel.serverSend({'type': 'pong'});
        channel.serverSend({'type': 'totally_unknown', 'x': 1});
        await Future.delayed(Duration.zero);
        expect(svc.pending, hasLength(1));
        svc.dispose();
      },
    );

    test(
      'a non-auth close clears connected and schedules a reconnect',
      () async {
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          // Long delay so the reconnect Timer never fires during the test
          // (dispose cancels it regardless).
          reconnectDelays: const [Duration(minutes: 5)],
        );
        await svc.connect();
        expect(svc.connected, isTrue);
        channel.serverClose(); // clean close, no code -> not an auth failure
        await Future.delayed(Duration.zero);
        expect(svc.connected, isFalse);
        expect(svc.authFailed, isFalse);
        svc.dispose();
      },
    );

    test(
      'egress_rules frame populates service.rules (sorted, parsed)',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        channel.serverSend(
          _rulesFrame(
            allowList: ['github.com', 'pypi.org'],
            allowed: [
              _ruleJson(id: 'a', decidedAt: 100, host: 'a.io'),
              _ruleJson(id: 'b', decidedAt: 200, host: 'b.io'),
            ],
            denied: [
              _ruleJson(
                id: 'd',
                decision: 'denied',
                host: 'x.io',
                decidedAt: 50,
              ),
            ],
          ),
        );
        await Future.delayed(Duration.zero);
        expect(svc.rules, isNotNull);
        expect(svc.rules!.allowList, ['github.com', 'pypi.org']);
        expect(svc.rules!.allowed.map((r) => r.id), ['b', 'a']); // newest first
        expect(svc.rules!.denied.single.id, 'd');
        svc.dispose();
      },
    );

    test('revoke_ack success removes the rule; failure flashes', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      channel.serverSend(
        _rulesFrame(allowed: [_ruleJson(id: 'a', decidedAt: 100)]),
      );
      await Future.delayed(Duration.zero);
      expect(svc.rules!.allowed, hasLength(1));
      channel.serverSend({'type': 'revoke_ack', 'request_id': 'a', 'ok': true});
      await Future.delayed(Duration.zero);
      expect(svc.rules!.allowed, isEmpty);
      // failure leaves the row in place and flashes
      channel.serverSend(
        _rulesFrame(
          denied: [_ruleJson(id: 'd', decision: 'denied', decidedAt: 100)],
        ),
      );
      await Future.delayed(Duration.zero);
      channel.serverSend({
        'type': 'revoke_ack',
        'request_id': 'd',
        'ok': false,
      });
      await Future.delayed(Duration.zero);
      expect(svc.rules!.denied, hasLength(1));
      expect(svc.flashMessage, contains('revoke failed'));
      svc.dispose();
    });

    test('revoke_ack ok before any rules is a no-op (no crash)', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      channel.serverSend({'type': 'revoke_ack', 'request_id': 'a', 'ok': true});
      await Future.delayed(Duration.zero);
      expect(svc.rules, isNull);
      svc.dispose();
    });

    test('sendRevoke sends a revoke frame on the socket', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendRevoke('v1');
      expect(channel.sent, isNotEmpty);
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out, {'type': 'revoke', 'request_id': 'v1'});
      svc.dispose();
    });

    test('sendRevoke flashes when disconnected', () {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.sendRevoke('v1');
      expect(channel.sent, isEmpty);
      expect(svc.flashMessage, contains('disconnected'));
      svc.dispose();
    });

    test('sendRevoke flashes when the socket send throws', () async {
      ConsentDeciderService.testChannelFactory = (_, __) => _ThrowingChannel();
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendRevoke('v1');
      expect(svc.flashMessage, contains('revoke send failed'));
      svc.dispose();
    });

    test('sendPause sends a pause frame and tracks the request', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      expect(svc.lastPauseRequest, isNull);
      svc.sendPause('1h');
      expect(channel.sent, isNotEmpty);
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out, {'type': 'pause', 'duration': '1h'});
      expect(svc.lastPauseRequest, '1h');
      svc.dispose();
    });

    test('sendUnpause sends an unpause frame and clears the request', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendPause('1h');
      svc.sendUnpause();
      expect(channel.sent, isNotEmpty);
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out, {'type': 'unpause'});
      expect(svc.lastPauseRequest, isNull);
      svc.dispose();
    });

    test('sendPause/sendUnpause flash when disconnected', () {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc.sendPause('15m');
      expect(channel.sent, isEmpty);
      expect(svc.flashMessage, contains('disconnected'));
      svc.dispose();
      final svc2 = ConsentDeciderService(workspaceId: 'ws', token: 't');
      svc2.sendUnpause();
      expect(svc2.flashMessage, contains('disconnected'));
      svc2.dispose();
    });

    test('sendPause/sendUnpause flash when the socket send throws', () async {
      ConsentDeciderService.testChannelFactory = (_, __) => _ThrowingChannel();
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendPause('15m');
      expect(svc.flashMessage, contains('pause send failed'));
      svc.dispose();
      final svc2 = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc2.connect();
      svc2.sendUnpause();
      expect(svc2.flashMessage, contains('unpause send failed'));
      svc2.dispose();
    });

    test(
      'pause_ack nack reverts the highlight and flashes which op failed',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        // A refused pause must not leave its button highlighted as active.
        svc.sendPause('1h');
        expect(svc.lastPauseRequest, '1h');
        channel.serverSend({'type': 'pause_ack', 'ok': false, 'until': null});
        await Future<void>.delayed(Duration.zero);
        expect(svc.flashMessage, contains('pause failed'));
        expect(svc.lastPauseRequest, isNull);
        // A refused unpause names itself in the flash.
        svc.sendUnpause();
        channel.serverSend({'type': 'pause_ack', 'ok': false, 'until': null});
        await Future<void>.delayed(Duration.zero);
        expect(svc.flashMessage, contains('unpause failed'));
        expect(svc.lastPauseRequest, isNull);
        svc.dispose();
      },
    );

    test(
      'pause_ack ok applies the acked window (authoritative fallback)',
      () async {
        final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
        await svc.connect();
        channel.serverSend(_rulesFrame(allowList: ['a.io']));
        await Future<void>.delayed(Duration.zero);
        expect(svc.rules!.paused, isNull);
        // The refreshed egress_rules broadcast normally lands first, but it is
        // best-effort server-side -- the ack's own until must apply alone.
        svc.sendPause('15m');
        channel.serverSend({'type': 'pause_ack', 'ok': true, 'until': 1300.0});
        await Future<void>.delayed(Duration.zero);
        expect(svc.rules!.paused, const EgressPause(until: 1300.0));
        expect(svc.flashMessage, isNull); // success never flashes
        // A successful unpause (ok, until null) clears the window.
        svc.sendUnpause();
        channel.serverSend({'type': 'pause_ack', 'ok': true, 'until': null});
        await Future<void>.delayed(Duration.zero);
        expect(svc.rules!.paused, isNull);
        svc.dispose();
      },
    );

    test('pause_ack ok before any rules snapshot is a safe no-op', () async {
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      svc.sendPause('15m');
      channel.serverSend({'type': 'pause_ack', 'ok': true, 'until': 1300.0});
      await Future<void>.delayed(Duration.zero);
      expect(svc.rules, isNull); // no crash; the connect snapshot carries truth
      expect(svc.lastPauseRequest, '15m');
      svc.dispose();
    });

    test(
      'ruleRemainingSeconds: timed -> value; open-ended/unknown/missing -> null',
      () {
        final now = DateTime.fromMillisecondsSinceEpoch(
          1000 * 1000,
          isUtc: true,
        );
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          clock: () => now,
        );
        // decided at 1000s, 5m (300s) -> 300s left at now=1000s
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'a',
              destHost: 'h',
              decision: 'allowed',
              duration: '5m',
              decidedAt: 1000.0,
            ),
          ),
          300,
        );
        // forever / tilrestart / unknown duration -> null
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'b',
              destHost: 'h',
              decision: 'allowed',
              duration: 'forever',
              decidedAt: 1000.0,
            ),
          ),
          isNull,
        );
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'c',
              destHost: 'h',
              decision: 'allowed',
              duration: 'tilrestart',
              decidedAt: 1000.0,
            ),
          ),
          isNull,
        );
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'd',
              destHost: 'h',
              decision: 'allowed',
              duration: 'bogus',
              decidedAt: 1000.0,
            ),
          ),
          isNull,
        );
        // missing decided_at -> null
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'e',
              destHost: 'h',
              decision: 'allowed',
              duration: '5m',
            ),
          ),
          isNull,
        );
        // clamped at 0 past expiry
        expect(
          svc.ruleRemainingSeconds(
            ConsentRule(
              id: 'f',
              destHost: 'h',
              decision: 'allowed',
              duration: '5m',
              decidedAt: 100.0,
            ),
          ),
          0,
        );
        svc.dispose();
      },
    );

    test(
      'pauseRemainingSeconds: null when not paused / indefinite; value when set',
      () {
        final now = DateTime.fromMillisecondsSinceEpoch(
          1000 * 1000,
          isUtc: true,
        );
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          clock: () => now,
        );
        final base = EgressRules(
          workspaceId: 'w',
          allowList: const [],
          allowed: const [],
          denied: const [],
        );
        expect(svc.pauseRemainingSeconds(base), isNull); // paused null
        expect(
          svc.pauseRemainingSeconds(
            EgressRules(
              workspaceId: 'w',
              allowList: const [],
              allowed: const [],
              denied: const [],
              paused: const EgressPause(),
            ),
          ),
          isNull,
        ); // until null
        expect(
          svc.pauseRemainingSeconds(
            EgressRules(
              workspaceId: 'w',
              allowList: const [],
              allowed: const [],
              denied: const [],
              paused: const EgressPause(until: 1300.0),
            ),
          ),
          300,
        ); // 1300 - 1000
        svc.dispose();
      },
    );
  });

  group('isRuleExpired', () {
    test(
      'timed past expiry -> true; open-ended / unknown / missing -> false',
      () {
        final now = DateTime.fromMillisecondsSinceEpoch(
          1000 * 1000,
          isUtc: true,
        );
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          clock: () => now,
        );
        ConsentRule rule(String? duration, {double? decidedAt}) => ConsentRule(
              id: 'r',
              destHost: 'h',
              decision: 'allowed',
              duration: duration,
              decidedAt: decidedAt,
            );
        // decided at 1000s, 5m (300s) -> expires at 1300s; now=1000s -> live.
        expect(svc.isRuleExpired(rule('5m', decidedAt: 1000.0)), isFalse);
        // decided at 100s -> expired long ago (remaining clamps to 0).
        expect(svc.isRuleExpired(rule('5m', decidedAt: 100.0)), isTrue);
        // open-ended / unknown / missing decided_at -> never expire.
        expect(svc.isRuleExpired(rule('forever', decidedAt: 100.0)), isFalse);
        expect(
          svc.isRuleExpired(rule('tilrestart', decidedAt: 100.0)),
          isFalse,
        );
        expect(svc.isRuleExpired(rule('bogus', decidedAt: 100.0)), isFalse);
        expect(svc.isRuleExpired(rule('5m')), isFalse);
        svc.dispose();
      },
    );
  });

  group('isPauseExpired', () {
    EgressRules rules(EgressPause? paused) => EgressRules(
          workspaceId: 'w',
          allowList: const [],
          allowed: const [],
          denied: const [],
          paused: paused,
        );

    test(
      'finite until past -> true; future / indefinite / not paused -> false',
      () {
        final now = DateTime.fromMillisecondsSinceEpoch(
          1000 * 1000,
          isUtc: true,
        );
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          clock: () => now,
        );
        // until 1300s, now 1000s -> live.
        expect(
          svc.isPauseExpired(rules(const EgressPause(until: 1300.0))),
          isFalse,
        );
        // until 900s -> elapsed.
        expect(
          svc.isPauseExpired(rules(const EgressPause(until: 900.0))),
          isTrue,
        );
        // Indefinite (until restart) and not-paused never expire.
        expect(
          svc.isPauseExpired(rules(const EgressPause(until: null))),
          isFalse,
        );
        expect(svc.isPauseExpired(rules(null)), isFalse);
        svc.dispose();
      },
    );
  });

  group('pruneExpiredRules', () {
    test('no-op (returns false) before the first egress_rules frame', () {
      final now = DateTime.fromMillisecondsSinceEpoch(1000 * 1000, isUtc: true);
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        clock: () => now,
      );
      expect(svc.pruneExpiredRules(), isFalse);
      expect(svc.rules, isNull);
      svc.dispose();
    });

    test('drops expired timed rules, keeps the rest, and notifies', () async {
      var now = DateTime.fromMillisecondsSinceEpoch(1000 * 1000, isUtc: true);
      final ch = _FakeChannel();
      ConsentDeciderService.testChannelFactory = (_, __) => ch;
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        clock: () => now,
      );
      await svc.connect();
      ch.serverSend(
        _rulesFrame(
          allowList: ['github.com'],
          allowed: [
            // timed: decided now (t=1000), 5m -> expires at t=1300; live for now.
            _ruleJson(id: 'timed', host: 't.io', decidedAt: 1000.0),
            // open-ended: never expires.
            _ruleJson(
              id: 'forever',
              host: 'f.io',
              duration: 'forever',
              decidedAt: 1.0,
            ),
          ],
          denied: [
            // timed deny: also expires at t=1300.
            _ruleJson(
              id: 'd-timed',
              host: 'dt.io',
              decision: 'denied',
              decidedAt: 1000.0,
            ),
          ],
        ),
      );
      await Future.delayed(Duration.zero); // flush the broadcast listener
      // Sorted newest-decided-first: 'timed' (1000) before 'forever' (1).
      expect(svc.rules!.allowed.map((r) => r.id), ['timed', 'forever']);
      expect(svc.rules!.denied.map((r) => r.id), ['d-timed']);
      expect(svc.rules!.allowList, ['github.com']);

      var notifications = 0;
      svc.addListener(() => notifications++);

      // Nothing has elapsed yet (clock still at t=1000): no-op, no notify.
      expect(svc.pruneExpiredRules(), isFalse);
      expect(notifications, 0);
      expect(svc.rules!.allowed.map((r) => r.id), ['timed', 'forever']);

      // Advance past the timed rules' expiry (t=1300) and prune again: both
      // timed rows drop, the open-ended allow stays, the allow-list is
      // preserved, and listeners were notified.
      now = DateTime.fromMillisecondsSinceEpoch(1400 * 1000, isUtc: true);
      expect(svc.pruneExpiredRules(), isTrue);
      expect(notifications, 1);
      expect(svc.rules!.allowed.map((r) => r.id), ['forever']);
      expect(svc.rules!.denied, isEmpty);
      expect(svc.rules!.allowList, ['github.com']);

      // Idempotent: pruning the already-pruned snapshot is a no-op.
      expect(svc.pruneExpiredRules(), isFalse);
      expect(notifications, 1);
      ConsentDeciderService.testChannelFactory = null;
      svc.dispose();
    });

    test(
      'drops a self-expired pause; keeps a live or indefinite one (#2494)',
      () async {
        var now = DateTime.fromMillisecondsSinceEpoch(1000 * 1000, isUtc: true);
        final ch = _FakeChannel();
        ConsentDeciderService.testChannelFactory = (_, __) => ch;
        final svc = ConsentDeciderService(
          workspaceId: 'ws',
          token: 't',
          clock: () => now,
        );
        await svc.connect();
        // Live window: until t=1300, now t=1000.
        ch.serverSend(_rulesFrame(paused: {'paused': true, 'until': 1300.0}));
        await Future.delayed(Duration.zero);
        expect(svc.pruneExpiredRules(), isFalse); // still live -> kept
        expect(svc.rules!.paused, const EgressPause(until: 1300.0));

        // Past expiry: the pause is pruned (never lingers at "resumes in 0s").
        now = DateTime.fromMillisecondsSinceEpoch(1400 * 1000, isUtc: true);
        expect(svc.pruneExpiredRules(), isTrue);
        expect(svc.rules!.paused, isNull);

        // An indefinite pause (until restart) never expires.
        ch.serverSend(_rulesFrame(paused: {'paused': true}));
        await Future.delayed(Duration.zero);
        expect(svc.pruneExpiredRules(), isFalse);
        expect(svc.rules!.paused, const EgressPause(until: null));
        ConsentDeciderService.testChannelFactory = null;
        svc.dispose();
      },
    );
  });
  group('ConsentDeciderService DPoP connect URI (#3218)', () {
    tearDown(() {
      testDpopBackendOverride = null;
    });

    test('bound token carries the dpop proof parameter', () async {
      testDpopBackendOverride = FakeDpopBackend(proof: 'dec-proof');
      final bound = boundToken();
      Uri? seen;
      List<String>? seenProtocols;
      ConsentDeciderService.testChannelFactory = (uri, protocols) {
        seen = uri;
        seenProtocols = protocols;
        return _FakeChannel();
      };

      final svc = ConsentDeciderService(workspaceId: 'ws', token: bound);
      await svc.connect();

      expect(seen!.queryParameters['dpop'], 'dec-proof');
      // #3201: the token rides the subprotocol list, never the query.
      expect(seenProtocols, ['bearer', bound]);
      expect(seen!.queryParameters.containsKey('token'), isFalse);
      expect(seen!.queryParameters['workspace'], 'ws');
      svc.dispose();
    });
  });

  group('ConsentDeciderService connect robustness (#3218 review)', () {
    tearDown(() {
      testDpopBackendOverride = null;
    });

    test('overlapping connects open exactly one channel', () async {
      var opened = 0;
      final gate = Completer<void>();
      ConsentDeciderService.testChannelFactory = (_, __) {
        opened += 1;
        // Hold the first (and only permitted) open until the second
        // connect() call has come and gone.
        return _FakeChannel();
      };

      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      final first = svc.connect();
      final second = svc.connect(); // must no-op on the in-flight guard
      await first;
      await second;
      await gate.future.timeout(const Duration(milliseconds: 10),
          onTimeout: () => gate.complete());
      expect(opened, 1);
      expect(svc.connected, isTrue);
      svc.dispose();
    });

    test('a throwing channel factory is contained and reconnects', () async {
      ConsentDeciderService.testChannelFactory =
          (_, __) => throw StateError('boom');
      final svc = ConsentDeciderService(workspaceId: 'ws', token: 't');
      await svc.connect();
      expect(svc.connected, isFalse);
      // The failure scheduled the normal reconnect backoff; a later
      // connect (the timer's path, or an explicit one) still works.
      ConsentDeciderService.testChannelFactory = (_, __) => _FakeChannel();
      await svc.connect();
      expect(svc.connected, isTrue);
      svc.dispose();
    });
  });
}
