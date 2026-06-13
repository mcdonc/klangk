import 'dart:async';

import 'package:flterm/flterm.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/terminal/ghostty_terminal.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _output = StreamController<String>.broadcast();
  final List<String> sentCommands = [];

  @override
  Stream<Map<String, dynamic>> get customEvents => _events.stream;

  @override
  Stream<String> get terminalOutput => _output.stream;

  @override
  String? get currentWorkspaceId => 'ws-1';

  void emitTerminal(String data) => _output.add(data);

  @override
  void sendTerminalStart({int? cols, int? rows}) {}

  @override
  void sendTerminalStop() {}

  @override
  void sendTerminalInput(String data) =>
      sentCommands.add('terminal_input:$data');

  @override
  void sendTerminalResize(int cols, int rows) {}

  void close() {
    _events.close();
    _output.close();
  }
}

Widget _build(_MockWsClient client, GlobalKey<GhosttyTerminalState> key) {
  // Pin a viewport so flterm has enough rows for a meaningful scrollback.
  return MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: 800,
        height: 600,
        child: GhosttyTerminal(key: key, wsClient: client),
      ),
    ),
  );
}

Future<void> _pumpReady(
  WidgetTester tester,
  _MockWsClient client,
  GlobalKey<GhosttyTerminalState> key,
) async {
  await tester.pumpWidget(_build(client, key));
  await tester.pumpAndSettle();
  key.currentState!.requestFocus();
  await tester.pump();
}

Future<void> _sendShiftPageKey(
  WidgetTester tester,
  LogicalKeyboardKey key,
) async {
  await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
  await tester.sendKeyEvent(key);
  await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
  await tester.pump();
}

void main() {
  setUp(() => testBaseUrlOverride = 'http://localhost:8997');
  tearDown(() => testBaseUrlOverride = null);

  group('GhosttyTerminal scrollback shortcuts (tmux-handled)', () {
    // Primary-screen scrollback is now handled by tmux (copy-mode via PgUp
    // bindings in tmux.conf). These tests verify that page keys reach the PTY
    // rather than being intercepted by Flutter.

    testWidgets('Shift+PgUp on primary screen reaches the PTY', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);

      final lines = List.generate(200, (i) => 'line $i').join('\r\n');
      client.emitTerminal('$lines\r\n');
      await tester.pumpAndSettle();
      client.sentCommands.clear();

      await _sendShiftPageKey(tester, LogicalKeyboardKey.pageUp);

      expect(client.sentCommands, isNotEmpty,
          reason: 'Shift+PgUp goes to PTY where tmux handles scrollback');
      client.close();
    });

    testWidgets('Shift+PgDn on primary screen reaches the PTY', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);

      final lines = List.generate(200, (i) => 'line $i').join('\r\n');
      client.emitTerminal('$lines\r\n');
      await tester.pumpAndSettle();
      client.sentCommands.clear();

      await _sendShiftPageKey(tester, LogicalKeyboardKey.pageDown);

      expect(client.sentCommands, isNotEmpty,
          reason: 'Shift+PgDn goes to PTY where tmux handles scrollback');
      client.close();
    });

    testWidgets('Shift+PgUp on alternate screen pages the app', (tester) async {
      // On the alt screen, Shift+PgUp is converted to mouse-wheel scroll
      // for apps like Pi that need that input type.
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);

      final lines = List.generate(200, (i) => 'line $i').join('\r\n');
      client.emitTerminal('$lines\r\n\x1b[?1049h');
      await tester.pumpAndSettle();

      final scroll = key.currentState!.scrollController;
      expect(scroll.activeScreen, TerminalScreen.alternate);
      final before = scroll.position.pixels;

      await _sendShiftPageKey(tester, LogicalKeyboardKey.pageUp);

      expect(
        scroll.position.pixels,
        before,
        reason:
            'scroll position must not change while the alt screen is active',
      );
      client.close();
    });
  });

  group('mouse wheel on alternate screen', () {
    testWidgets('wheel up sends PgUp to PTY on alt screen', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);

      // Switch to alt screen (tmux uses this)
      client.emitTerminal('\x1b[?1049h');
      await tester.pumpAndSettle();
      expect(key.currentState!.scrollController.activeScreen,
          TerminalScreen.alternate);
      client.sentCommands.clear();

      // Send wheel up event
      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(
        PointerScrollEvent(
            position: center, scrollDelta: const Offset(0, -100)),
      );
      await tester.pumpAndSettle();

      // Should have sent PgUp (ESC [5~) to the PTY
      expect(
        client.sentCommands.any((c) => c.contains('\x1b[5~')),
        isTrue,
        reason: 'wheel up on alt screen sends PgUp to PTY for tmux scrollback',
      );
      client.close();
    });

    testWidgets('wheel down sends PgDn to PTY on alt screen', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);

      client.emitTerminal('\x1b[?1049h');
      await tester.pumpAndSettle();
      client.sentCommands.clear();

      final center = tester.getCenter(find.byType(TerminalView));
      await tester.sendEventToBinding(
        PointerScrollEvent(position: center, scrollDelta: const Offset(0, 100)),
      );
      await tester.pumpAndSettle();

      expect(
        client.sentCommands.any((c) => c.contains('\x1b[6~')),
        isTrue,
        reason:
            'wheel down on alt screen sends PgDn to PTY for tmux scrollback',
      );
      client.close();
    });
  });
}
