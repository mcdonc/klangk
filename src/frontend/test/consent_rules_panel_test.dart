import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/consent_decider_service.dart';
import 'package:klangk_frontend/workspace/consent_rules_panel.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

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

Map<String, dynamic> _ruleJson({
  String id = 'v1',
  String host = 'a.io',
  int? port = 443,
  String? proc = 'curl',
  String decision = 'allowed',
  String? duration = '5m',
  double? decidedAt = 1000.0,
}) =>
    {
      'id': id,
      'dest_host': host,
      if (port != null) 'dest_port': port,
      if (proc != null) 'process_name': proc,
      'decision': decision,
      if (duration != null) 'duration': duration,
      if (decidedAt != null) 'decided_at': decidedAt,
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

DateTime _at(int epochSec) =>
    DateTime.fromMillisecondsSinceEpoch(epochSec * 1000, isUtc: true);

ConsentDeciderService _serviceWithChannel(
  _FakeChannel channel, {
  DateTime Function()? clock,
}) {
  ConsentDeciderService.testChannelFactory = (_, __) => channel;
  return ConsentDeciderService(
    workspaceId: 'ws',
    token: 't',
    clock: clock ?? _wallClock,
  );
}

DateTime _wallClock() => DateTime.now();

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  tearDown(() {
    ConsentDeciderService.testChannelFactory = null;
  });

  group('formatDurationCompact', () {
    test('<60s -> Ns', () => expect(formatDurationCompact(5), '5s'));
    test('<1h -> Nm', () => expect(formatDurationCompact(125), '2m'));
    test('<1d -> Nh', () => expect(formatDurationCompact(7200), '2h'));
    test('<1w -> Nd', () => expect(formatDurationCompact(90000), '1d'));
    test('>=1w -> Nw', () => expect(formatDurationCompact(700000), '1w'));
  });

  group('ruleExpiryLabel', () {
    ConsentRule rule(String? duration, {double? decidedAt}) => ConsentRule(
          id: 'r',
          destHost: 'h',
          decision: 'allowed',
          duration: duration,
          decidedAt: decidedAt,
        );

    test('forever', () {
      expect(ruleExpiryLabel(rule('forever'), null, deny: false), 'forever');
    });
    test('tilrestart', () {
      expect(
        ruleExpiryLabel(rule('tilrestart'), null, deny: false),
        'until restart',
      );
    });
    test('allow timed -> expires in', () {
      expect(ruleExpiryLabel(rule('5m'), 300, deny: false), 'expires in 5m');
    });
    test('deny timed -> left', () {
      expect(ruleExpiryLabel(rule('5m'), 300, deny: true), '5m left');
    });
    test('timed but null remaining -> empty', () {
      expect(ruleExpiryLabel(rule('5m'), null, deny: false), '');
    });
  });

  group('hasLiveCountdown', () {
    int? remaining(ConsentRule r) => r.duration == '5m' ? 5 : null;
    final timed = ConsentRule(
      id: 'a',
      destHost: 'h',
      decision: 'allowed',
      duration: '5m',
      decidedAt: 1.0,
    );
    final forever = ConsentRule(
      id: 'b',
      destHost: 'h',
      decision: 'allowed',
      duration: 'forever',
      decidedAt: 1.0,
    );
    EgressRules rules(List<ConsentRule> allowed, {EgressPause? paused}) =>
        EgressRules(
          workspaceId: 'w',
          allowList: const [],
          allowed: allowed,
          denied: const [],
          paused: paused,
        );

    test(
      'null rules -> false',
      () => expect(hasLiveCountdown(null, remaining), isFalse),
    );
    test('only forever/tilrestart rules -> false', () {
      expect(hasLiveCountdown(rules([forever]), remaining), isFalse);
    });
    test('a timed rule -> true', () {
      expect(hasLiveCountdown(rules([timed]), remaining), isTrue);
    });
    test('a pause -> true', () {
      expect(
        hasLiveCountdown(
          rules([forever], paused: const EgressPause(until: 1.0)),
          remaining,
        ),
        isTrue,
      );
    });
  });

  group('ConsentRulesPanel', () {
    testWidgets('shows the loading state before the first frame', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      expect(find.text('Loading consent rules…'), findsOneWidget);
      svc.dispose();
    });

    testWidgets('renders allow-list + allows + denies sections with labels', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch, clock: () => _at(1000));
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(
          allowList: ['github.com'],
          allowed: [
            _ruleJson(
              id: 'a',
              host: 'f.io',
              duration: 'forever',
              decidedAt: 100,
            ),
            _ruleJson(
              id: 'b',
              host: 't.io',
              duration: 'tilrestart',
              decidedAt: 100,
            ),
            _ruleJson(id: 'c', host: 'm.io', duration: '5m', decidedAt: 1000),
            _ruleJson(
              id: 'd',
              host: 'n.io',
              port: null,
              proc: null,
              duration: '5m',
              decidedAt: null,
            ),
          ],
          denied: const [],
        ),
      );
      await tester.pump();
      expect(find.text('Static allow-list'), findsOneWidget);
      expect(find.text('github.com'), findsOneWidget);
      expect(find.text('Active allows (4)'), findsOneWidget);
      expect(find.text('Active denies (0)'), findsOneWidget);
      // empty denies + empty reject-list (the frame carries no reject_list)
      expect(find.text('(none)'), findsNWidgets(2));
      // four revocable allow rows (label branches: forever/tilrestart/timed/empty)
      expect(find.byKey(const ValueKey('revoke-a')), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-b')), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-c')), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-d')), findsOneWidget);
      expect(find.textContaining('connected'), findsOneWidget);
      svc.dispose();
    });

    testWidgets('revoke confirms then sends (with process in the prompt)', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(
          allowed: [_ruleJson(id: 'v1', host: 'a.io', proc: 'curl')],
        ),
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('revoke-v1')));
      await tester.pump();
      await tester.pump();
      expect(find.text('Revoke consent rule?'), findsOneWidget);
      expect(find.textContaining('a.io:443'), findsOneWidget);
      expect(find.textContaining('(curl)'), findsOneWidget);
      await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
      await tester.pump();
      expect(jsonDecode(ch.sent.last as String), {
        'type': 'revoke',
        'request_id': 'v1',
      });
      svc.dispose();
    });

    testWidgets('revoke prompt for a rule with no process', (tester) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(
          allowed: [_ruleJson(id: 'v2', host: 'b.io', proc: null)],
        ),
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('revoke-v2')));
      await tester.pump();
      await tester.pump();
      expect(find.textContaining('b.io:443'), findsOneWidget);
      await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
      await tester.pump();
      expect(jsonDecode(ch.sent.last as String), {
        'type': 'revoke',
        'request_id': 'v2',
      });
      svc.dispose();
    });

    testWidgets('revoke prompt for a denied rule says deny', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(
          denied: [_ruleJson(id: 'v4', host: 'd.io', decision: 'denied')],
        ),
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('revoke-v4')));
      await tester.pump();
      await tester.pump();
      expect(
        find.textContaining('Remove the deny rule for d.io:443'),
        findsOneWidget,
      );
      await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
      await tester.pump();
      expect(jsonDecode(ch.sent.last as String), {
        'type': 'revoke',
        'request_id': 'v4',
      });
      svc.dispose();
    });

    testWidgets('cancel does not send a revoke', (tester) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowed: [_ruleJson(id: 'v3')]));
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('revoke-v3')));
      await tester.pump();
      await tester.pump();
      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pump();
      expect(ch.sent, isEmpty);
      svc.dispose();
    });

    testWidgets('shows the service flash row', (tester) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowList: ['a.io']));
      await tester.pump();
      ch.serverSend({'type': 'error', 'message': 'verdict rejected'});
      await tester.pump();
      expect(find.text('verdict rejected'), findsOneWidget);
      svc.dispose();
    });

    testWidgets('renders the pause section (countdown then until-restart)', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch, clock: () => _at(1000));
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(paused: {'paused': true, 'until': 1300.0}));
      await tester.pump();
      expect(find.textContaining('resumes in 5m'), findsOneWidget);
      // switch to an indefinite pause
      ch.serverSend(_rulesFrame(paused: {'paused': true}));
      await tester.pump();
      expect(find.text('Filtering paused until restart'), findsOneWidget);
      svc.dispose();
    });

    testWidgets(
      'pause controls render once rules land; highlight follows the last acked request (#2494)',
      (tester) async {
        final ch = _FakeChannel();
        final svc = _serviceWithChannel(ch);
        svc.connect();
        await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
        // No controls before the first egress_rules frame (loading state).
        expect(find.text('Pause prompts'), findsNothing);
        ch.serverSend(_rulesFrame(allowList: ['a.io']));
        await tester.pump();

        // All four buttons render (Unpause / Pause 15m / 1h / 1d, mirroring
        // the TUI pause bar); nothing is paused so there is no status line.
        expect(find.text('Pause prompts'), findsOneWidget);
        expect(find.text('Unpause'), findsOneWidget);
        expect(find.text('Pause 15m'), findsOneWidget);
        expect(find.text('Pause 1h'), findsOneWidget);
        expect(find.text('Pause 1d'), findsOneWidget);
        expect(find.textContaining('Filtering paused'), findsNothing);

        // Unpause is the active (filled) button when nothing was last paused.
        expect(
          tester.widget(find.byKey(const ValueKey('pause-none'))),
          isA<FilledButton>(),
        );
        expect(
          tester.widget(find.byKey(const ValueKey('pause-15m'))),
          isA<OutlinedButton>(),
        );

        // Tapping the active button sends too (unpause is idempotent
        // server-side) and keeps Unpause as the last request.
        await tester.tap(find.byKey(const ValueKey('pause-none')));
        await tester.pump();
        expect(jsonDecode(ch.sent.last as String), {'type': 'unpause'});

        // Tapping Pause 15m sends the frame and moves the highlight.
        await tester.tap(find.byKey(const ValueKey('pause-15m')));
        await tester.pump();
        expect(jsonDecode(ch.sent.last as String), {
          'type': 'pause',
          'duration': '15m',
        });
        expect(
          tester.widget(find.byKey(const ValueKey('pause-15m'))),
          isA<FilledButton>(),
        );
        expect(
          tester.widget(find.byKey(const ValueKey('pause-none'))),
          isA<OutlinedButton>(),
        );

        // A refused pause (nack) reverts the highlight: the button must not
        // claim a window the server never set.
        ch.serverSend({'type': 'pause_ack', 'ok': false, 'until': null});
        await tester.pump();
        expect(find.text('pause failed'), findsOneWidget);
        expect(
          tester.widget(find.byKey(const ValueKey('pause-none'))),
          isA<FilledButton>(),
        );
        expect(
          tester.widget(find.byKey(const ValueKey('pause-15m'))),
          isA<OutlinedButton>(),
        );

        // A successful pause applies from the ack alone (the egress_rules
        // re-broadcast is best-effort server-side): the status line renders.
        await tester.tap(find.byKey(const ValueKey('pause-1h')));
        await tester.pump();
        expect(jsonDecode(ch.sent.last as String), {
          'type': 'pause',
          'duration': '1h',
        });
        ch.serverSend({'type': 'pause_ack', 'ok': true, 'until': 1300.0});
        await tester.pump();
        expect(find.textContaining('Filtering paused'), findsOneWidget);

        // Tapping Unpause sends the frame; a successful ack clears the status.
        await tester.tap(find.byKey(const ValueKey('pause-none')));
        await tester.pump();
        expect(jsonDecode(ch.sent.last as String), {'type': 'unpause'});
        ch.serverSend({'type': 'pause_ack', 'ok': true, 'until': null});
        await tester.pump();
        expect(find.textContaining('Filtering paused'), findsNothing);
        expect(
          tester.widget(find.byKey(const ValueKey('pause-none'))),
          isA<FilledButton>(),
        );
        svc.dispose();
      },
    );

    testWidgets(
      'a self-expired pause disappears on the next tick (never "resumes in 0s")',
      (tester) async {
        var now = _at(1000); // epoch seconds
        final ch = _FakeChannel();
        final svc = _serviceWithChannel(ch, clock: () => now);
        svc.connect();
        await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
        // Paused until t=1300; now t=1000 -> live countdown shows.
        ch.serverSend(_rulesFrame(paused: {'paused': true, 'until': 1300.0}));
        await tester.pump();
        expect(find.textContaining('Filtering paused'), findsOneWidget);

        // Past the window: before the 1s tick fires the (cached) status line
        // still renders; the tick must prune it, not freeze at "resumes in 0s"
        // -- the server only re-broadcasts on the next discrete event.
        now = _at(1400);
        await tester.pump();
        expect(find.textContaining('Filtering paused'), findsOneWidget);
        await tester.pump(const Duration(seconds: 1));
        expect(find.textContaining('Filtering paused'), findsNothing);
        // The controls stay rendered for the next request.
        expect(find.text('Pause prompts'), findsOneWidget);
        svc.dispose();
      },
    );

    testWidgets('pause tap while disconnected flashes instead of sending', (
      tester,
    ) async {
      final ch = _FakeChannel();
      ConsentDeciderService.testChannelFactory = (_, __) => ch;
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        // Long delay so the reconnect Timer never fires during the test.
        reconnectDelays: const [Duration(minutes: 5)],
      );
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowList: ['a.io']));
      await tester.pump();
      ch.serverClose();
      await tester.pump();
      await tester.pump(); // flush onDone -> reconnecting
      await tester.tap(find.byKey(const ValueKey('pause-1h')));
      await tester.pump();
      expect(ch.sent, isEmpty); // nothing left the socket
      expect(find.textContaining('disconnected'), findsOneWidget); // the flash
      svc.dispose();
    });

    testWidgets('a failed pause_ack flashes via the service flash row', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowList: ['a.io']));
      await tester.pump();
      ch.serverSend({'type': 'pause_ack', 'ok': false, 'until': null});
      await tester.pump();
      expect(find.text('pause failed'), findsOneWidget);
      // The controls stay rendered (state unchanged, never optimistic).
      expect(find.text('Pause 15m'), findsOneWidget);
      svc.dispose();
    });

    testWidgets('a revoked row stays until revoke_ack succeeds', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(allowed: [_ruleJson(id: 'v1', decidedAt: 100)]),
      );
      await tester.pump();
      expect(find.byKey(const ValueKey('revoke-v1')), findsOneWidget);
      // confirm revoke -> frame sent, but the row stays (never optimistic)
      await tester.tap(find.byKey(const ValueKey('revoke-v1')));
      await tester.pump();
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
      await tester.pump();
      expect(jsonDecode(ch.sent.last as String), {
        'type': 'revoke',
        'request_id': 'v1',
      });
      expect(find.byKey(const ValueKey('revoke-v1')), findsOneWidget);
      // server acks success -> row leaves
      ch.serverSend({'type': 'revoke_ack', 'request_id': 'v1', 'ok': true});
      await tester.pump();
      expect(find.byKey(const ValueKey('revoke-v1')), findsNothing);
      svc.dispose();
    });

    testWidgets('static allow-list rows are not revocable', (tester) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowList: ['github.com', 'pypi.org']));
      await tester.pump();
      expect(find.text('github.com'), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-github.com')), findsNothing);
      expect(find.byKey(const ValueKey('revoke-pypi.org')), findsNothing);
      svc.dispose();
    });

    testWidgets(
      'static reject-list section renders below the allow-list and is read-only (#2503)',
      (tester) async {
        final ch = _FakeChannel();
        final svc = _serviceWithChannel(ch);
        svc.connect();
        await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
        ch.serverSend(
          _rulesFrame(
            allowList: ['github.com'],
            rejectList: ['bad.io', 'evil.io:443'],
          ),
        );
        await tester.pump();
        // Both static sections render, reject-list after the allow-list.
        expect(find.text('Static allow-list'), findsOneWidget);
        expect(find.text('Static reject-list'), findsOneWidget);
        expect(find.text('bad.io'), findsOneWidget);
        expect(find.text('evil.io:443'), findsOneWidget);
        final allowIdx = tester
            .widgetList<Text>(find.byType(Text))
            .toList()
            .indexWhere((t) => t.data == 'Static allow-list');
        final rejectIdx = tester
            .widgetList<Text>(find.byType(Text))
            .toList()
            .indexWhere((t) => t.data == 'Static reject-list');
        expect(rejectIdx, greaterThan(allowIdx));
        // Workspace config, not consent rows: no revoke affordance, and an
        // empty list shows the shared (none) placeholder.
        expect(find.byKey(const ValueKey('revoke-bad.io')), findsNothing);
        ch.serverSend(_rulesFrame(rejectList: []));
        await tester.pump();
        expect(find.text('Static reject-list'), findsOneWidget);
        svc.dispose();
      },
    );

    testWidgets('confirm after the panel is disposed does not send (#2393)', (
      tester,
    ) async {
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(
        _rulesFrame(allowed: [_ruleJson(id: 'v1', decidedAt: 100)]),
      );
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('revoke-v1')));
      await tester.pump();
      await tester.pump();
      // The confirm dialog lives on the root navigator, so it outlives the
      // panel: tearing the panel down (e.g. navigate away) while it is open
      // must NOT let a later Revoke tap reach the disposed service.
      await tester.pumpWidget(_wrap(const SizedBox.shrink()));
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Revoke'));
      await tester.pump();
      expect(ch.sent, isEmpty); // the mounted guard short-circuited sendRevoke
      svc.dispose();
    });

    testWidgets('header shows reconnecting when disconnected', (tester) async {
      final ch = _FakeChannel();
      ConsentDeciderService.testChannelFactory = (_, __) => ch;
      final svc = ConsentDeciderService(
        workspaceId: 'ws',
        token: 't',
        // Long delay so the reconnect Timer never fires during the test
        // (dispose cancels it regardless).
        reconnectDelays: const [Duration(minutes: 5)],
      );
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      ch.serverSend(_rulesFrame(allowList: ['a.io']));
      await tester.pump();
      ch.serverClose();
      await tester.pump();
      await tester.pump(); // flush onDone -> notifyListeners -> rebuild
      expect(find.textContaining('reconnecting'), findsOneWidget);
      svc.dispose();
    });

    testWidgets('an expired timed rule disappears on the next tick (#2467)', (
      tester,
    ) async {
      var now = _at(1000); // epoch seconds
      final ch = _FakeChannel();
      final svc = _serviceWithChannel(ch, clock: () => now);
      svc.connect();
      await tester.pumpWidget(_wrap(ConsentRulesPanel(service: svc)));
      // A timed allow (decided now, expires in 5m / at t=1300) + a forever
      // allow (no expiry).
      ch.serverSend(
        _rulesFrame(
          allowed: [
            _ruleJson(
              id: 'timed',
              host: 't.io',
              duration: '5m',
              decidedAt: 1000,
            ),
            _ruleJson(
              id: 'forever',
              host: 'f.io',
              duration: 'forever',
              decidedAt: 1.0,
            ),
          ],
        ),
      );
      await tester.pump();
      expect(find.text('Active allows (2)'), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-timed')), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-forever')), findsOneWidget);

      // Past the timed rule's expiry. Before the 1s tick fires the row is
      // still the cached snapshot (no rebuild has pruned it yet).
      now = _at(1400);
      await tester.pump();
      expect(find.byKey(const ValueKey('revoke-timed')), findsOneWidget);

      // The 1s tick prunes the elapsed rule: it vanishes, the count drops,
      // and the open-ended rule stays (no "0s left" linger).
      await tester.pump(const Duration(seconds: 1));
      expect(find.text('Active allows (1)'), findsOneWidget);
      expect(find.byKey(const ValueKey('revoke-timed')), findsNothing);
      expect(find.byKey(const ValueKey('revoke-forever')), findsOneWidget);
      svc.dispose();
    });
  });
}
