import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/consent_banner.dart';
import 'package:klangk_frontend/workspace/consent_decider_service.dart';
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

ConsentDeciderService _serviceWithChannel(_FakeChannel channel) {
  ConsentDeciderService.testChannelFactory = (_) => channel;
  return ConsentDeciderService(
    workspaceId: 'ws',
    token: 't',
    // Fixed clock so the countdown is deterministic.
    clock: () => DateTime.fromMillisecondsSinceEpoch(2000 * 1000, isUtc: true),
  );
}

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  tearDown(() {
    ConsentDeciderService.testChannelFactory = null;
  });

  testWidgets('renders nothing when there are no pending requests', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pumpAndSettle();
    expect(find.textContaining('Pending egress consent'), findsNothing);
    svc.dispose();
  });

  testWidgets('shows the host + Allow/Deny for a pending request', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'process_name': 'curl',
        // requested 0s ago at the fixed clock (epoch 2000s).
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    expect(find.textContaining('example.com:443'), findsOneWidget);
    expect(find.text('Allow'), findsOneWidget);
    expect(find.text('Deny'), findsOneWidget);
    svc.dispose();
  });

  testWidgets('tapping Allow sends an allow verdict on the socket', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    await tester.tap(find.text('Allow'));
    await tester.pump();
    expect(channel.sent, isNotEmpty);
    final out = jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
    expect(out['type'], 'verdict');
    expect(out['request_id'], 'r1');
    expect(out['decision'], 'allowed');
    svc.dispose();
  });

  testWidgets('tapping Deny sends a deny verdict on the socket', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r2',
        'workspace_id': 'ws',
        'dest_host': 'bad.io',
        'dest_port': 22,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    await tester.tap(find.text('Deny'));
    await tester.pump();
    final out = jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
    expect(out['decision'], 'denied');
    expect(out['request_id'], 'r2');
    svc.dispose();
  });

  testWidgets('Allow carries the default duration (tilrestart)', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    await tester.tap(find.text('Allow'));
    await tester.pump();
    final out = jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
    expect(out['duration'], 'tilrestart');
    svc.dispose();
  });

  testWidgets(
    'the global duration selector is one button per duration (#2499)',
    (tester) async {
      final channel = _FakeChannel();
      final svc = _serviceWithChannel(channel);
      svc.connect();
      channel.serverSend({
        'type': 'egress_request',
        'request': {
          'id': 'r1',
          'workspace_id': 'ws',
          'dest_host': 'example.com',
          'dest_port': 443,
          'requested_at': 2000.0,
        },
      });
      await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
      await tester.pump();
      // No dropdown anywhere; one button per selectable duration (TUI parity),
      // and the test-only 5s is not offered (#2487).
      expect(find.byType(DropdownButton<String>), findsNothing);
      expect(find.byKey(const ValueKey('dur-5s')), findsNothing);
      for (final d in kConsentDurations) {
        expect(find.byKey(ValueKey('dur-$d')), findsOneWidget);
      }
      // The default (tilrestart) is the active filled button; others outlined.
      expect(
        tester.widget(find.byKey(const ValueKey('dur-tilrestart'))),
        isA<FilledButton>(),
      );
      expect(
        tester.widget(find.byKey(const ValueKey('dur-1d'))),
        isA<OutlinedButton>(),
      );
      svc.dispose();
    },
  );

  testWidgets('selecting a duration applies it to the next Allow', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    // Tap the already-active button (tilrestart): a no-op reselect that
    // keeps it active -- and exercises the filled button's onPressed.
    await tester.tap(find.byKey(const ValueKey('dur-tilrestart')));
    await tester.pump();
    expect(tester.widget(find.byKey(const ValueKey('dur-tilrestart'))),
        isA<FilledButton>());

    // Tap the 1d duration button -- selecting does NOT submit, it only moves
    // the highlight; the verdict still needs an Allow/Deny tap.
    await tester.tap(find.byKey(const ValueKey('dur-1d')));
    await tester.pump();
    expect(
      tester.widget(find.byKey(const ValueKey('dur-1d'))),
      isA<FilledButton>(),
    );
    expect(
      tester.widget(find.byKey(const ValueKey('dur-tilrestart'))),
      isA<OutlinedButton>(),
    );
    // The chosen duration now rides on the verdict.
    await tester.tap(find.text('Allow'));
    await tester.pump();
    final out = jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
    expect(out['duration'], '1d');
    svc.dispose();
  });

  testWidgets('shows a flash for a server error frame', (tester) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    channel.serverSend({'type': 'error', 'message': 'verdict rejected'});
    await tester.pump();
    await tester.pump();
    expect(find.textContaining('verdict rejected'), findsOneWidget);
    svc.dispose();
  });

  testWidgets('shows a re-login notice when the session auth-failed', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    svc.connect();
    channel.serverClose(4001);
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pumpAndSettle();
    expect(find.textContaining('please log in again'), findsOneWidget);
    svc.dispose();
  });

  testWidgets('shows "reconnecting…" when disconnected mid-hold', (
    tester,
  ) async {
    final channel = _FakeChannel();
    ConsentDeciderService.testChannelFactory = (_) => channel;
    final svc = ConsentDeciderService(
      workspaceId: 'ws',
      token: 't',
      // Long delay so the reconnect Timer never fires during the test
      // (dispose cancels it regardless).
      reconnectDelays: const [Duration(minutes: 5)],
      clock: () =>
          DateTime.fromMillisecondsSinceEpoch(2000 * 1000, isUtc: true),
    );
    svc.connect();
    channel.serverSend({
      'type': 'egress_request',
      'request': {
        'id': 'r1',
        'workspace_id': 'ws',
        'dest_host': 'example.com',
        'dest_port': 443,
        'requested_at': 2000.0,
      },
    });
    await tester.pumpWidget(_wrap(ConsentBanner(service: svc)));
    await tester.pump();
    expect(find.text('reconnecting…'), findsNothing); // still connected
    channel.serverClose(); // clean drop, no code -> not an auth failure
    await tester.pump(); // flush onDone -> notifyListeners -> rebuild
    await tester.pump();
    expect(find.text('reconnecting…'), findsOneWidget);
    svc.dispose();
  });
}
