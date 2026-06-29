import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/terminal/ghostty_terminal.dart';
import 'package:klangk_frontend/workspace/terminal_tabs_view.dart';
import 'package:klangk_frontend/ws/ws_client.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _output = StreamController<String>.broadcast();
  final List<String> sentCommands = [];

  List<Map<String, dynamic>> _windows = [];
  List<Map<String, dynamic>> _shared = [];
  String? _userId;

  @override
  Stream<Map<String, dynamic>> get customEvents => _events.stream;

  @override
  Stream<String> get terminalOutput => _output.stream;

  @override
  String? get currentWorkspaceId => 'ws-1';

  @override
  String? get currentUserId => _userId;

  @override
  List<Map<String, dynamic>> get terminalWindows => _windows;

  @override
  List<Map<String, dynamic>> get sharedTerminals => _shared;

  @override
  void sendTerminalStart({int? cols, int? rows}) =>
      sentCommands.add('terminal_start');

  @override
  void sendTerminalInput(String data) =>
      sentCommands.add('terminal_input:$data');

  @override
  void sendTerminalResize(int cols, int rows) =>
      sentCommands.add('terminal_resize:${cols}x$rows');

  @override
  void sendTerminalStop() => sentCommands.add('terminal_stop');

  @override
  void sendTerminalCloseWindow(int index) =>
      sentCommands.add('close_window:$index');

  @override
  void sendTerminalRenameWindow(int index, String name) =>
      sentCommands.add('rename_window:$index:$name');

  @override
  void sendTerminalNewWindow({String? name}) => sentCommands.add('new_window');

  @override
  void sendShareWindow(String windowId) => sentCommands.add('share:$windowId');

  @override
  void sendUnshareWindow(String windowId) =>
      sentCommands.add('unshare:$windowId');

  @override
  void sendJoinSharedTerminal(String userId, String windowId) =>
      sentCommands.add('join:$userId:$windowId');

  @override
  void sendTerminalSelectWindow(String windowId) =>
      sentCommands.add('select_window:$windowId');

  void close() {
    _events.close();
    _output.close();
  }
}

Widget _build(
  _MockWsClient client, {
  String? selectedOwnWindowId,
  Map<String, String>? activeSharedTerminal,
  bool Function(String)? hasPerm,
  void Function(WsClient, String)? onSwitchToIsolated,
  void Function(WsClient, String, String)? onJoinShared,
}) {
  return MaterialApp(
    home: Scaffold(
      body: TerminalTabsView(
        wsClient: client,
        terminalKey: GlobalKey<GhosttyTerminalState>(),
        onPathTap: (_) {},
        selectedOwnWindowId: selectedOwnWindowId,
        activeSharedTerminal: activeSharedTerminal,
        hasPerm: hasPerm ?? (_) => true,
        onSwitchToIsolated: onSwitchToIsolated ?? (_, __) {},
        onJoinShared: onJoinShared ?? (_, __, ___) {},
      ),
    ),
  );
}

void main() {
  group('TerminalTabsView', () {
    testWidgets('shows no tab bar when no windows or shared terminals',
        (tester) async {
      final client = _MockWsClient();
      client._windows = [];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // The GhosttyTerminal should still be present
      expect(find.byType(GhosttyTerminal), findsOneWidget);
      // No tab bar container (height 32)
      expect(find.text('New terminal'), findsNothing);
    });

    testWidgets('renders own terminal tabs when code-in-isolation perm',
        (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
        {'id': 'w2', 'name': 'vim', 'index': 1, 'active': false},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      expect(find.text('bash'), findsOneWidget);
      expect(find.text('vim'), findsOneWidget);
    });

    testWidgets('hides own tabs when code-in-isolation perm is missing',
        (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(
        client,
        hasPerm: (p) => p != 'code-in-isolation',
      ));

      expect(find.text('bash'), findsNothing);
    });

    testWidgets('shows + button only with code-in-isolation', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      expect(find.byIcon(Icons.add), findsOneWidget);
    });

    testWidgets('tapping + creates new window', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      await tester.tap(find.byIcon(Icons.add));
      assert(client.sentCommands.contains('new_window'));
    });

    testWidgets('tapping own tab calls onSwitchToIsolated', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': false},
        {'id': 'w2', 'name': 'vim', 'index': 1, 'active': false},
      ];
      client._shared = [];
      String? switchedTo;
      await tester.pumpWidget(_build(
        client,
        onSwitchToIsolated: (_, windowId) => switchedTo = windowId,
      ));

      await tester.tap(find.text('vim'));
      expect(switchedTo, 'w2');
    });

    testWidgets('shows shared terminals from other users', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [];
      client._shared = [
        {
          'user_id': 'user-2',
          'window_id': 'w-other',
          'handle': 'alice',
          'window_name': 'main',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      await tester.pumpWidget(_build(
        client,
        hasPerm: (p) => p != 'code-in-isolation',
      ));

      expect(find.text('alice:mai…'), findsOneWidget);
    });

    testWidgets('tapping shared tab calls onJoinShared', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [];
      client._shared = [
        {
          'user_id': 'user-2',
          'window_id': 'w-other',
          'handle': 'bob',
          'window_name': 'top',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      String? joinedUser;
      String? joinedWindow;
      await tester.pumpWidget(_build(
        client,
        hasPerm: (p) => p != 'code-in-isolation',
        onJoinShared: (_, userId, windowId) {
          joinedUser = userId;
          joinedWindow = windowId;
        },
      ));

      await tester.tap(find.text('bob:top'));
      expect(joinedUser, 'user-2');
      expect(joinedWindow, 'w-other');
    });

    testWidgets('close button shown only when multiple windows',
        (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Single window — no close icon
      expect(find.byIcon(Icons.close), findsNothing);

      // Add a second window
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
        {'id': 'w2', 'name': 'vim', 'index': 1, 'active': false},
      ];
      await tester.pumpWidget(_build(client));
      await tester.pump();

      // Multiple windows — close icons appear
      expect(find.byIcon(Icons.close), findsWidgets);
    });

    testWidgets('shows viewer count when viewers present', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [];
      client._shared = [
        {
          'user_id': 'user-2',
          'window_id': 'w-other',
          'handle': 'alice',
          'window_name': 'main',
          'viewers': [
            {'email': 'bob@test.com'},
            {'email': 'carol@test.com'},
          ],
        },
      ];
      await tester.pumpWidget(_build(
        client,
        hasPerm: (p) => p != 'code-in-isolation',
      ));

      expect(find.text('2'), findsOneWidget);
      expect(find.byIcon(Icons.visibility), findsOneWidget);
    });
  });
}
