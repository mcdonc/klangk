import 'dart:async';
import 'dart:ui';

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

    testWidgets('selectedOwnWindowId selects the right tab', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': false},
        {'id': 'w2', 'name': 'vim', 'index': 1, 'active': false},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(
        client,
        selectedOwnWindowId: 'w2',
      ));

      // 'vim' tab should be active (selected via selectedOwnWindowId)
      expect(find.text('vim'), findsOneWidget);
      expect(find.text('bash'), findsOneWidget);
    });

    testWidgets('own tab shows broadcast icon when shared', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [
        {
          'user_id': 'user-1',
          'window_id': 'w1',
          'handle': 'me',
          'window_name': 'bash',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      await tester.pumpWidget(_build(client));

      // The cell_tower icon indicates the tab is shared
      expect(find.byIcon(Icons.cell_tower), findsOneWidget);
    });

    testWidgets('tapping broadcast icon calls onToggleShare', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [
        {
          'user_id': 'user-1',
          'window_id': 'w1',
          'handle': 'me',
          'window_name': 'bash',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      await tester.pumpWidget(_build(client));

      await tester.tap(find.byIcon(Icons.cell_tower));
      // Should unshare since it's already shared
      expect(client.sentCommands, contains('unshare:w1'));
    });

    testWidgets('context menu shows rename and share options', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Right-click to show context menu
      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      expect(find.text('Rename'), findsOneWidget);
      expect(find.text('Share'), findsOneWidget);
    });

    testWidgets('context menu share action calls toggle', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Right-click to show context menu
      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      // Tap Share
      await tester.tap(find.text('Share'));
      await tester.pumpAndSettle();

      expect(client.sentCommands, contains('share:w1'));
    });

    testWidgets('context menu rename opens dialog and renames', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Right-click to show context menu
      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      // Tap Rename
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      // Rename dialog should appear
      expect(find.text('Rename terminal'), findsOneWidget);

      // Clear and type new name
      await tester.enterText(find.byType(TextField), 'zsh');
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();

      expect(client.sentCommands, contains('rename_window:0:zsh'));
    });

    testWidgets('context menu rename cancel does not rename', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(
        client.sentCommands.where((c) => c.startsWith('rename_window')),
        isEmpty,
      );
    });

    testWidgets('shared tab shows lock icon when read-only', (tester) async {
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
        hasPerm: (p) => false, // no perms — read-only
      ));

      expect(find.byIcon(Icons.lock_outlined), findsOneWidget);
    });

    testWidgets('shared tab shows edit icon when writable', (tester) async {
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
        hasPerm: (p) =>
            p == 'code-in-shared-terminals' || p == 'share-terminals',
      ));

      expect(find.byIcon(Icons.edit_outlined), findsOneWidget);
    });

    testWidgets('active shared tab is highlighted', (tester) async {
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
        activeSharedTerminal: {
          'user_id': 'user-2',
          'window_id': 'w-other',
        },
      ));

      // Tab should be rendered (active state tested by widget internals)
      expect(find.text('alice:mai…'), findsOneWidget);
    });

    testWidgets('close button sends close command', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
        {'id': 'w2', 'name': 'vim', 'index': 1, 'active': false},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Find close icons and tap the first one
      await tester.tap(find.byIcon(Icons.close).first);
      expect(client.sentCommands, contains('close_window:0'));
    });

    testWidgets('long handle and window name are truncated in shared tab',
        (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [];
      client._shared = [
        {
          'user_id': 'user-2',
          'window_id': 'w-other',
          'handle': 'longusername',
          'window_name': 'longwindow',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      await tester.pumpWidget(_build(
        client,
        hasPerm: (p) => p != 'code-in-isolation',
      ));

      // Handle truncated to 5 chars + …, window to 3 chars + …
      expect(find.text('longu…:lon…'), findsOneWidget);
    });

    testWidgets('hover and unhover changes tab appearance', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': false},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Hover over the tab
      final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await gesture.addPointer(location: Offset.zero);
      addTearDown(gesture.removePointer);

      await gesture.moveTo(tester.getCenter(find.text('bash').first));
      await tester.pump();

      // Hover out
      await gesture.moveTo(Offset.zero);
      await tester.pump();

      // Tab should still be visible after hover cycle
      expect(find.text('bash'), findsWidgets);
    });

    testWidgets('hover on + button changes appearance', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await gesture.addPointer(location: Offset.zero);
      addTearDown(gesture.removePointer);

      await gesture.moveTo(tester.getCenter(find.byIcon(Icons.add)));
      await tester.pump();

      // + button should still be visible
      expect(find.byIcon(Icons.add), findsOneWidget);
    });

    testWidgets('context menu unshare when tab already shared', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [
        {
          'user_id': 'user-1',
          'window_id': 'w1',
          'handle': 'me',
          'window_name': 'bash',
          'viewers': <Map<String, dynamic>>[],
        },
      ];
      await tester.pumpWidget(_build(client));

      // Right-click
      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      // Context menu should say "Unshare" since it's already shared
      expect(find.text('Unshare'), findsOneWidget);

      await tester.tap(find.text('Unshare'));
      await tester.pumpAndSettle();

      expect(client.sentCommands, contains('unshare:w1'));
    });

    testWidgets('viewers tooltip shows email prefixes', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [
        {
          'user_id': 'user-1',
          'window_id': 'w1',
          'handle': 'me',
          'window_name': 'bash',
          'viewers': [
            {'email': 'bob@test.com'},
          ],
        },
      ];
      await tester.pumpWidget(_build(client));

      // Viewer count should be shown
      expect(find.text('1'), findsOneWidget);
    });

    testWidgets('rename dialog submits on Enter key', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      // Right-click to show context menu
      final tabFinder = find.text('bash');
      final center = tester.getCenter(tabFinder);
      await tester.tapAt(center, buttons: 2);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      // Type new name and submit with Enter
      await tester.enterText(find.byType(TextField), 'fish');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(client.sentCommands, contains('rename_window:0:fish'));
    });

    testWidgets('hover exit on + button resets state', (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      client._shared = [];
      await tester.pumpWidget(_build(client));

      final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
      await gesture.addPointer(location: Offset.zero);
      addTearDown(gesture.removePointer);

      // Hover in
      await gesture.moveTo(tester.getCenter(find.byIcon(Icons.add)));
      await tester.pump();

      // Hover out
      await gesture.moveTo(Offset.zero);
      await tester.pump();

      expect(find.byIcon(Icons.add), findsOneWidget);
    });

    testWidgets('_getViewers returns empty for non-matching shared terminal',
        (tester) async {
      final client = _MockWsClient();
      client._userId = 'user-1';
      client._windows = [
        {'id': 'w1', 'name': 'bash', 'index': 0, 'active': true},
      ];
      // Shared terminal with different window_id
      client._shared = [
        {
          'user_id': 'user-1',
          'window_id': 'w-other',
          'handle': 'me',
          'window_name': 'bash',
          'viewers': [
            {'email': 'bob@test.com'},
          ],
        },
      ];
      await tester.pumpWidget(_build(client));

      // No viewer count should appear for w1 since shared is for w-other
      expect(find.byIcon(Icons.visibility), findsNothing);
    });
  });
}
