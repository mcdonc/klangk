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
  ConsentDeciderService.testChannelFactory = (_, __) => channel;
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
    await svc.connect();
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
    await svc.connect();
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
    await svc.connect();
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
    await svc.connect();
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
    // A bare Deny click carries the default duration, like Allow.
    expect(out['duration'], 'tilrestart');
    svc.dispose();
  });

  testWidgets('Allow carries the default duration (tilrestart)', (
    tester,
  ) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    await svc.connect();
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
    'the Allow ▾ menu offers every duration and submits the choice',
    (tester) async {
      final channel = _FakeChannel();
      final svc = _serviceWithChannel(channel);
      await svc.connect();
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
      // The #2499 global pill row is gone; durations live on the row's menu.
      expect(find.byKey(const ValueKey('dur-1d')), findsNothing);
      // Open the menu anchored to the ▾ segment.
      await tester.tap(find.byKey(const ValueKey('allow-dur-r1')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      // Every selectable duration is offered (human labels, default marked);
      // the test-only 5s is not (#2487).
      expect(find.text('Just once'), findsOneWidget);
      expect(find.text('1 day'), findsOneWidget);
      expect(find.text('Until restart (default)'), findsOneWidget);
      expect(find.text('Forever'), findsOneWidget);
      expect(find.text('5 seconds'), findsNothing);
      // Dismissing without a pick sends nothing.
      await tester.tapAt(const Offset(5, 5));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(channel.sent, isEmpty);
      // Picking a duration submits the verdict with it immediately.
      await tester.tap(find.byKey(const ValueKey('allow-dur-r1')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.text('1 day'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out['type'], 'verdict');
      expect(out['request_id'], 'r1');
      expect(out['decision'], 'allowed');
      expect(out['duration'], '1d');
      svc.dispose();
    },
  );

  testWidgets(
    'the Deny ▾ menu submits a deny verdict with the chosen duration',
    (tester) async {
      final channel = _FakeChannel();
      final svc = _serviceWithChannel(channel);
      await svc.connect();
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
      await tester.tap(find.byKey(const ValueKey('deny-dur-r2')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.text('Forever'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out['decision'], 'denied');
      expect(out['request_id'], 'r2');
      expect(out['duration'], 'forever');
      svc.dispose();
    },
  );

  testWidgets(
    'the duration menu stays on screen at phone widths',
    (tester) async {
      tester.view.physicalSize = const Size(420, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final channel = _FakeChannel();
      final svc = _serviceWithChannel(channel);
      await svc.connect();
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
      await tester.tap(find.byKey(const ValueKey('allow-dur-r1')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      // Regression (PR review): an overlay-relative position keeps every
      // menu item inside the viewport even when the ▾ sits right of the
      // viewport's midpoint (the Expanded host label pushes it there).
      final item = find.text('Forever');
      expect(item, findsOneWidget);
      expect(tester.getTopLeft(item).dx, greaterThanOrEqualTo(0));
      // On-screen means actually tappable: the pick lands on the socket.
      await tester.tap(item);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      final out =
          jsonDecode(channel.sent.last as String) as Map<String, dynamic>;
      expect(out['duration'], 'forever');
      svc.dispose();
    },
  );

  testWidgets(
    'the menu closes itself if its request resolves while open',
    (tester) async {
      final channel = _FakeChannel();
      final svc = _serviceWithChannel(channel);
      await svc.connect();
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
      await tester.tap(find.byKey(const ValueKey('allow-dur-r1')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('Forever'), findsOneWidget);
      // The row resolves (another decider / timeout) while the menu is
      // open: the menu must close and no verdict may be sent afterwards.
      channel.serverSend({'type': 'egress_resolved', 'request_id': 'r1'});
      await tester.pump(); // flush the stream event -> listener -> menu pop
      await tester.pumpAndSettle(); // exit transition fully unmounts
      expect(find.text('Forever'), findsNothing);
      expect(channel.sent, isEmpty);
      svc.dispose();
    },
  );

  testWidgets('shows a flash for a server error frame', (tester) async {
    final channel = _FakeChannel();
    final svc = _serviceWithChannel(channel);
    await svc.connect();
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
    await svc.connect();
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
    ConsentDeciderService.testChannelFactory = (_, __) => channel;
    final svc = ConsentDeciderService(
      workspaceId: 'ws',
      token: 't',
      // Long delay so the reconnect Timer never fires during the test
      // (dispose cancels it regardless).
      reconnectDelays: const [Duration(minutes: 5)],
      clock: () =>
          DateTime.fromMillisecondsSinceEpoch(2000 * 1000, isUtc: true),
    );
    await svc.connect();
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
