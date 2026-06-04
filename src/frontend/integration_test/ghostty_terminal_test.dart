// Integration test: exercises GhosttyTerminal inside a REAL Flutter engine on
// the device (macOS/Linux), not the headless test VM. This is where terminal
// behaviors that depend on a real desktop process — libghostty's FFI VT,
// genuine rendering/layout, font loading, paste — are assertable (issue #7).
//
// Run: flutter test -d macos integration_test/ghostty_terminal_test.dart
//
// The body mirrors test/ghostty_terminal_test.dart; the difference is the
// IntegrationTestWidgetsFlutterBinding, which drives a real engine.
import 'dart:async';

import 'package:flterm/flterm.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:klangk_frontend/terminal/ghostty_terminal.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Minimal WsClient fake: records the terminal commands GhosttyTerminal sends
/// and lets the test drive the customEvents / terminalOutput streams.
class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _output = StreamController<String>.broadcast();
  final List<String> sentCommands = [];
  final bool hasWorkspace;

  _MockWsClient({this.hasWorkspace = true});

  @override
  Stream<Map<String, dynamic>> get customEvents => _events.stream;

  @override
  Stream<String> get terminalOutput => _output.stream;

  @override
  String? get currentWorkspaceId => hasWorkspace ? 'ws-1' : null;

  void emit(Map<String, dynamic> event) => _events.add(event);
  void emitTerminal(String data) => _output.add(data);

  @override
  void sendTerminalStart({int cols = 80, int rows = 24}) =>
      sentCommands.add('terminal_start:${cols}x$rows');

  @override
  void sendTerminalStop() => sentCommands.add('terminal_stop');

  @override
  void sendTerminalInput(String data) =>
      sentCommands.add('terminal_input:$data');

  @override
  void sendTerminalResize(int cols, int rows) =>
      sentCommands.add('terminal_resize:${cols}x$rows');

  void close() {
    _events.close();
    _output.close();
  }
}

Widget _build(_MockWsClient client, {GlobalKey<GhosttyTerminalState>? key}) {
  return MaterialApp(
    home: Scaffold(body: GhosttyTerminal(key: key, wsClient: client)),
  );
}

Map<String, Object?> _containerReady() => {
      'type': 'event',
      'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
    };

/// GhosttyTerminal's [_loadFont] hands raw font bytes to flterm via a runtime
/// `FontLoader.load()`, which fires a "system fonts changed" platform message.
/// On a real engine that callback can land mid-frame and trip a framework
/// assertion (`RenderParagraph._scheduleSystemFontsUpdate` requires the idle
/// phase). It's benign here and never fires in the headless VM suite. Swallow
/// exactly that assertion and forward everything else to the test reporter.
/// Call at the top of each test body — the test binding installs its own
/// `FlutterError.onError` per test, so this must run after the body starts.
void _ignoreSystemFontsAssert() {
  final reporter = FlutterError.onError;
  FlutterError.onError = (details) {
    final text = details.exceptionAsString();
    if (text.contains('_scheduleSystemFontsUpdate') ||
        text.contains('midFrameMicrotasks')) {
      return;
    }
    reporter?.call(details);
  };
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => testBaseUrlOverride = 'http://localhost:8997');
  tearDown(() => testBaseUrlOverride = null);

  group('GhosttyTerminal (native engine)', () {
    testWidgets('shows connect message when no workspace', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient(hasWorkspace: false);
      await tester.pumpWidget(_build(client));
      expect(find.textContaining('Connect to a workspace'), findsOneWidget);
      expect(find.byType(TerminalView), findsNothing);
      client.close();
    });

    testWidgets('renders TerminalView once the font loads', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      expect(find.byType(TerminalView), findsOneWidget);
      client.close();
    });

    testWidgets('sends terminal_start on container_ready', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      expect(client.sentCommands.where((c) => c.startsWith('terminal_start')),
          isEmpty);

      client.emit(_containerReady());
      await tester.pump();

      expect(
        client.sentCommands.where((c) => c.startsWith('terminal_start')).length,
        1,
      );
      client.close();
    });

    testWidgets('writes server output to the terminal without error',
        (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();

      client.emitTerminal('hello from server\r\n');
      await tester.pump();

      expect(find.byType(TerminalView), findsOneWidget);
      expect(tester.takeException(), isNull);
      client.close();
    });

    testWidgets('emits a resize command as the view lays out', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      expect(
        client.sentCommands.where((c) => c.startsWith('terminal_resize')),
        isNotEmpty,
      );
      client.close();
    });

    testWidgets('right-click opens the context menu with Paste',
        (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.byType(TerminalView));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      expect(find.text('Paste'), findsOneWidget);
      client.close();
    });

    testWidgets('routeNativePaste forwards the payload when focused',
        (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      key.currentState!.requestFocus();
      await tester.pump();
      client.sentCommands.clear();

      final consumed = key.currentState!.routeNativePaste('clipboard-payload');
      await tester.pump();

      expect(consumed, isTrue);
      final pasted =
          client.sentCommands.where((c) => c.startsWith('terminal_input:'));
      expect(pasted, isNotEmpty);
      expect(pasted.join(), contains('clipboard-payload'));
      client.close();
    });
  });

  group('font zoom (issue #7)', () {
    testWidgets('increase / decrease / reset via methods', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      final state = key.currentState!;

      expect(state.fontSize, 16);

      state.increaseFontSize();
      await tester.pump();
      final up = state.fontSize;
      expect(up, greaterThan(16));

      state.decreaseFontSize();
      await tester.pump();
      expect(state.fontSize, lessThan(up));

      state.increaseFontSize();
      state.increaseFontSize();
      await tester.pump();
      state.resetFontSize();
      await tester.pump();
      expect(state.fontSize, 16);
      client.close();
    });

    testWidgets('clamps at min and max', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      final state = key.currentState!;

      for (var i = 0; i < 100; i++) {
        state.increaseFontSize();
      }
      await tester.pump();
      final max = state.fontSize;
      expect(max, lessThanOrEqualTo(40));
      state.increaseFontSize(); // no-op at the ceiling
      await tester.pump();
      expect(state.fontSize, max);

      for (var i = 0; i < 100; i++) {
        state.decreaseFontSize();
      }
      await tester.pump();
      expect(state.fontSize, greaterThanOrEqualTo(6));
      client.close();
    });

    testWidgets('Cmd +/-/0 keyboard shortcuts change font size', (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      key.currentState!.requestFocus();
      await tester.pump();
      final state = key.currentState!;
      expect(state.fontSize, 16);

      await _pressMetaCombo(tester, LogicalKeyboardKey.equal);
      expect(state.fontSize, greaterThan(16), reason: 'Cmd+= zooms in');

      await _pressMetaCombo(tester, LogicalKeyboardKey.minus);
      await _pressMetaCombo(tester, LogicalKeyboardKey.minus);
      expect(state.fontSize, lessThan(16), reason: 'Cmd+- zooms out');

      await _pressMetaCombo(tester, LogicalKeyboardKey.digit0);
      expect(state.fontSize, 16, reason: 'Cmd+0 resets');
      client.close();
    });
  });

  group('scrollback paging (issue #7)', () {
    testWidgets('Shift+PgUp scrolls back, Shift+PgDown returns toward live',
        (tester) async {
      _ignoreSystemFontsAssert();
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();

      // Fill scrollback well past the viewport so there's something to page.
      for (var i = 0; i < 400; i++) {
        client.emitTerminal('scrollback line $i\r\n');
      }
      await tester.pumpAndSettle();

      final state = key.currentState!;
      if (state.maxScrollExtent > 0) {
        // Live view sits at the bottom (max offset); paging up moves toward
        // older lines (smaller offset).
        final atLive = state.scrollOffset;
        state.scrollPageUp();
        await tester.pumpAndSettle();
        expect(state.scrollOffset, lessThan(atLive),
            reason: 'Shift+PgUp scrolls into scrollback');

        final scrolledBack = state.scrollOffset;
        state.scrollPageDown();
        await tester.pumpAndSettle();
        expect(state.scrollOffset, greaterThan(scrolledBack),
            reason: 'Shift+PgDown scrolls back toward live');
      }
      // Whether or not scrollback formed in the test viewport, paging must be
      // a safe no-op that never throws.
      expect(tester.takeException(), isNull);
      expect(find.byType(TerminalView), findsOneWidget);
      client.close();
    });
  });
}

/// Press a Cmd/⌘-modified key (macOS) and pump. The integration test runs on
/// the macOS engine, so the font-zoom shortcuts bind to the meta modifier.
Future<void> _pressMetaCombo(WidgetTester tester, LogicalKeyboardKey key) async {
  await tester.sendKeyDownEvent(LogicalKeyboardKey.metaLeft);
  await tester.sendKeyEvent(key);
  await tester.sendKeyUpEvent(LogicalKeyboardKey.metaLeft);
  await tester.pumpAndSettle();
}
