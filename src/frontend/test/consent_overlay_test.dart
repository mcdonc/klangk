import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/consent/consent_decider_client.dart';
import 'package:klangk_frontend/consent/consent_overlay.dart';
import 'package:klangk_frontend/consent/consent_request.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// A controllable fake client: lets tests set state directly and capture
/// verdict calls without a real socket.
class _FakeClient extends ConsentDeciderClient {
  _FakeClient(AuthService auth) : super(workspaceId: 'ws-1', auth: auth);

  final List<Map<String, String>> verdicts = [];

  @override
  bool connected = false;

  @override
  bool get connecting => false;

  @override
  bool get authFailed => false;

  final Map<String, ConsentRequest> _pending = {};

  void addRequest(ConsentRequest req) {
    _pending[req.id] = req;
    notifyListeners();
  }

  void clearPending() {
    _pending.clear();
    notifyListeners();
  }

  @override
  List<ConsentRequest> get pending => _pending.values.toList()
    ..sort((a, b) => a.requestedAt.compareTo(b.requestedAt));

  @override
  bool get hasPending => _pending.isNotEmpty;

  @override
  double remaining(ConsentRequest req) => 42.0;

  @override
  void allow(String requestId) =>
      verdicts.add({'request_id': requestId, 'decision': 'allowed'});

  @override
  void deny(String requestId) =>
      verdicts.add({'request_id': requestId, 'decision': 'denied'});

  @override
  Future<void> connect() async {}

  @override
  void dispose() {
    super.dispose();
  }
}

/// Minimal fake WebSocketChannel for the production-path test (the widget's
/// own client connects through this).
class _OverlayFakeChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _OverlaySink();

  @override
  int? get closeCode => null;

  @override
  Future<void> get ready => Future.value();
}

class _OverlaySink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) {}

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

void main() {
  late AuthService auth;

  setUp(() async {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
    auth = AuthService();
    await Future.delayed(Duration.zero);
    // Don't trip the real channel factory in any overlay created without an
    // injected client.
    ConsentDeciderClient.testChannelFactory = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    ConsentDeciderClient.testChannelFactory = null;
  });

  Widget _wrap(Widget child) =>
      MaterialApp(home: Scaffold(body: Stack(children: [child])));

  testWidgets('collapsed chip shows idle egress label when connected',
      (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    // Idle: shows "Egress: 0 held".
    expect(find.textContaining('Egress'), findsOneWidget);
    expect(find.byIcon(Icons.shield_outlined), findsOneWidget);
    // Pause button present.
    expect(
        find.byTooltip('Pause egress filtering (unavailable)'), findsOneWidget);
  });

  testWidgets('collapsed chip shows auto-deny warning when disconnected',
      (tester) async {
    final client = _FakeClient(auth)..connected = false;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    expect(find.textContaining('Auto-deny'), findsOneWidget);
    expect(find.byIcon(Icons.warning_amber_rounded), findsOneWidget);
  });

  testWidgets('pause button shows unavailable snackbar', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    await tester.tap(find.byTooltip('Pause egress filtering (unavailable)'));
    await tester.pumpAndSettle();
    expect(find.textContaining('not yet available'), findsOneWidget);
  });

  testWidgets('expands when a held request arrives', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'example.com',
      destPort: 443,
      processName: 'curl',
      pid: 7,
      requestedAt: 0,
    ));
    await tester.pump();
    expect(find.text('example.com:443'), findsOneWidget);
    expect(find.text('curl'), findsOneWidget);
    expect(find.text('Allow'), findsOneWidget);
    expect(find.text('Deny'), findsOneWidget);
    expect(find.textContaining('1 egress request held'), findsOneWidget);
  });

  testWidgets('collapses back when no requests are pending', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'h',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    expect(find.text('Allow'), findsOneWidget);
    client.clearPending();
    await tester.pump();
    expect(find.text('Allow'), findsNothing);
    expect(find.textContaining('Egress'), findsOneWidget);
  });

  testWidgets('Allow button sends an allow verdict', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'h',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    await tester.tap(find.text('Allow'));
    expect(client.verdicts, [
      {'request_id': 'r1', 'decision': 'allowed'},
    ]);
  });

  testWidgets('Deny button sends a deny verdict', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r2',
      workspaceId: 'ws-1',
      destHost: 'h',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    await tester.tap(find.text('Deny'));
    expect(client.verdicts, [
      {'request_id': 'r2', 'decision': 'denied'},
    ]);
  });

  testWidgets('renders host without port when port is null', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'noport.example',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    expect(find.text('noport.example'), findsOneWidget);
  });

  testWidgets('shows countdown seconds', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'h',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    // remaining() is stubbed to 42.0 -> ceil -> "42s".
    expect(find.text('42s'), findsOneWidget);
  });

  testWidgets('pluralizes header for multiple held requests', (tester) async {
    final client = _FakeClient(auth)..connected = true;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'a',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 1,
    ));
    client.addRequest(ConsentRequest(
      id: 'r2',
      workspaceId: 'ws-1',
      destHost: 'b',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 2,
    ));
    await tester.pump();
    expect(find.textContaining('2 egress requests held'), findsOneWidget);
  });

  testWidgets('expanded panel shows disconnected warning in header',
      (tester) async {
    final client = _FakeClient(auth)..connected = false;
    await tester.pumpWidget(_wrap(ConsentOverlay(
      workspaceId: 'ws-1',
      client: client,
    )));
    await tester.pump();
    client.addRequest(ConsentRequest(
      id: 'r1',
      workspaceId: 'ws-1',
      destHost: 'h',
      destPort: null,
      processName: null,
      pid: null,
      requestedAt: 0,
    ));
    await tester.pump();
    expect(find.byTooltip('Disconnected — held requests auto-deny'),
        findsOneWidget);
  });

  testWidgets('production path: creates its own client from AuthService',
      (tester) async {
    // A fake channel the widget's own client connects through.
    final channel = _OverlayFakeChannel();
    ConsentDeciderClient.testChannelFactory = (_) => channel;
    await tester.pumpWidget(
      MaterialApp(
        home: ChangeNotifierProvider<AuthService>.value(
          value: auth,
          child: const Scaffold(
            body: Stack(children: [ConsentOverlay(workspaceId: 'ws-1')]),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    // The widget connected through the fake factory and renders the
    // collapsed idle chip.
    expect(find.textContaining('Egress'), findsOneWidget);
    ConsentDeciderClient.testChannelFactory = null;
  });
}
