import 'dart:async';

import 'package:flterm/flterm.dart';
import 'package:flutter/foundation.dart'
    show TargetPlatform, debugDefaultTargetPlatformOverride;
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
  final List<String> sentInput = [];

  @override
  Stream<Map<String, dynamic>> get customEvents => _events.stream;

  @override
  Stream<String> get terminalOutput => _output.stream;

  @override
  String? get currentWorkspaceId => 'ws-1';

  void emitTerminal(String data) => _output.add(data);

  @override
  void sendTerminalStart({int cols = 80, int rows = 24}) {}

  @override
  void sendTerminalStop() {}

  @override
  void sendTerminalInput(String data) => sentInput.add(data);

  @override
  void sendTerminalResize(int cols, int rows) {}

  void close() {
    _events.close();
    _output.close();
  }
}

Widget _build(_MockWsClient client, GlobalKey<GhosttyTerminalState> key) {
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

Future<void> _fillPrimary(WidgetTester tester, _MockWsClient client) async {
  final lines = List.generate(200, (i) => 'line $i').join('\r\n');
  client.emitTerminal('$lines\r\n');
  await tester.pumpAndSettle();
}

Future<void> _sendKey(
  WidgetTester tester,
  LogicalKeyboardKey key, {
  LogicalKeyboardKey? modifier,
}) async {
  await _sendKeyMods(tester, key,
      modifiers: modifier == null ? const [] : [modifier]);
}

Future<void> _sendKeyMods(
  WidgetTester tester,
  LogicalKeyboardKey key, {
  List<LogicalKeyboardKey> modifiers = const [],
}) async {
  for (final m in modifiers) {
    await tester.sendKeyDownEvent(m);
  }
  await tester.sendKeyEvent(key);
  for (final m in modifiers.reversed) {
    await tester.sendKeyUpEvent(m);
  }
  await tester.pump();
}

void main() {
  setUp(() => testBaseUrlOverride = 'http://localhost:8997');
  tearDown(() {
    testBaseUrlOverride = null;
    GhosttyTerminalState.isWebOverride = false;
    debugDefaultTargetPlatformOverride = null;
  });

  group('plain PageUp/PageDown routing', () {
    testWidgets('native: PageUp is forwarded to the PTY', (tester) async {
      GhosttyTerminalState.isWebOverride = false;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await _fillPrimary(tester, client);
      final before = key.currentState!.scrollController.position.pixels;
      client.sentInput.clear();

      await _sendKey(tester, LogicalKeyboardKey.pageUp);

      expect(client.sentInput, isNotEmpty,
          reason: 'on native, PageUp is encoded and sent to the PTY');
      expect(key.currentState!.scrollController.position.pixels, before);
      client.close();
    });

    testWidgets('web + alternate screen: PageUp is forwarded to the PTY', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      // Switch to the alt screen (CSI ?1049h).
      client.emitTerminal('\x1b[?1049h');
      await tester.pumpAndSettle();
      expect(key.currentState!.scrollController.activeScreen,
          TerminalScreen.alternate);
      client.sentInput.clear();

      await _sendKey(tester, LogicalKeyboardKey.pageUp);

      expect(client.sentInput, isNotEmpty,
          reason: 'alt screen (vim/less) must still receive PageUp');
      client.close();
    });

    testWidgets('web + primary screen: PageUp is forwarded to the PTY', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await _fillPrimary(tester, client);
      client.sentInput.clear();

      await _sendKey(tester, LogicalKeyboardKey.pageUp);

      expect(client.sentInput, isNotEmpty,
          reason: 'PageUp goes to PTY where tmux handles scrollback');
      client.close();
    });

    // NOTE: Shift+PageUp on web primary is NOT tested — flterm converts it
    // into mouse-wheel scroll events rather than the key sequence. Plain
    // PageUp works correctly for tmux copy-mode scrollback.

    testWidgets('web + primary screen: Ctrl+PageUp still goes to the PTY', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await _fillPrimary(tester, client);
      client.sentInput.clear();

      await _sendKey(tester, LogicalKeyboardKey.pageUp,
          modifier: LogicalKeyboardKey.control);

      expect(client.sentInput, isNotEmpty,
          reason: 'a modified PageUp is not a browser key; forward to the PTY');
      client.close();
    });
  });

  group('page-scroll keys', () {
    Future<void> switchToAltScreen(
      WidgetTester tester,
      _MockWsClient client,
      GlobalKey<GhosttyTerminalState> key,
    ) async {
      client.emitTerminal('\x1b[?1049h');
      await tester.pumpAndSettle();
      expect(key.currentState!.scrollController.activeScreen,
          TerminalScreen.alternate);
    }

    testWidgets('Shift+PgUp/PgDn page the app on the alt screen', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await switchToAltScreen(tester, client, key);

      // No terminal scrollback on the alt screen; _scrollAltScreenByPage hands
      // the app a page of mouse-wheel scroll (flterm handleScroll), which reaches
      // the PTY. _bypassKey releases the combo to our shortcut so flterm doesn't
      // encode it first under the app's keyboard protocol.
      client.sentInput.clear();
      await _sendKey(tester, LogicalKeyboardKey.pageUp,
          modifier: LogicalKeyboardKey.shift);
      await tester.pumpAndSettle();
      expect(client.sentInput, isNotEmpty,
          reason: 'Shift+PgUp on the alt screen pages the app via the PTY');

      client.sentInput.clear();
      await _sendKey(tester, LogicalKeyboardKey.pageDown,
          modifier: LogicalKeyboardKey.shift);
      await tester.pumpAndSettle();
      expect(client.sentInput, isNotEmpty,
          reason: 'Shift+PgDn on the alt screen pages the app via the PTY');
      client.close();
    });

    testWidgets('macOS Cmd+PgUp pages the app on the alt screen', (
      tester,
    ) async {
      debugDefaultTargetPlatformOverride = TargetPlatform.macOS;
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await switchToAltScreen(tester, client, key);

      client.sentInput.clear();
      await _sendKey(tester, LogicalKeyboardKey.pageUp,
          modifier: LogicalKeyboardKey.meta);
      await tester.pumpAndSettle();
      expect(client.sentInput, isNotEmpty,
          reason: 'Cmd+PgUp on the alt screen pages the app on macOS');
      debugDefaultTargetPlatformOverride = null;
      client.close();
    });

    testWidgets('alt screen: typing reaches the PTY', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      await switchToAltScreen(tester, client, key);

      // Typing on the alt screen reaches the PTY normally.
      client.sentInput.clear();
      await _sendKey(tester, LogicalKeyboardKey.keyA);
      await tester.pumpAndSettle();
      expect(client.sentInput, isNotEmpty,
          reason: 'the keystroke is encoded to the PTY on the alt screen');
      client.close();
    });
  });

  group('font zoom', () {
    testWidgets('native: Ctrl +/-/0 zoom and reset, with clamping', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = false;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      final state = key.currentState!;
      expect(state.fontSize, 16);

      await _sendKey(tester, LogicalKeyboardKey.equal,
          modifier: LogicalKeyboardKey.control);
      expect(state.fontSize, 18);

      await _sendKey(tester, LogicalKeyboardKey.minus,
          modifier: LogicalKeyboardKey.control);
      await _sendKey(tester, LogicalKeyboardKey.minus,
          modifier: LogicalKeyboardKey.control);
      expect(state.fontSize, 14);

      await _sendKey(tester, LogicalKeyboardKey.digit0,
          modifier: LogicalKeyboardKey.control);
      expect(state.fontSize, 16);

      // Clamp at the maximum (40) — 13 steps of +2 from 16 would reach 42.
      for (var i = 0; i < 13; i++) {
        await _sendKey(tester, LogicalKeyboardKey.equal,
            modifier: LogicalKeyboardKey.control);
      }
      expect(state.fontSize, 40);

      // Clamp at the minimum (8).
      for (var i = 0; i < 20; i++) {
        await _sendKey(tester, LogicalKeyboardKey.minus,
            modifier: LogicalKeyboardKey.control);
      }
      expect(state.fontSize, 8);
      client.close();
    });

    testWidgets('web: Ctrl+= does not zoom the font (browser owns zoom)', (
      tester,
    ) async {
      GhosttyTerminalState.isWebOverride = true;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await _pumpReady(tester, client, key);
      final state = key.currentState!;

      await _sendKey(tester, LogicalKeyboardKey.equal,
          modifier: LogicalKeyboardKey.control);

      expect(state.fontSize, 16,
          reason: 'on web the zoom shortcuts are not bound; browser zooms');
      client.close();
    });
  });

  group('zoom modifier is platform-aware', () {
    KeyEvent zoomKey(LogicalKeyboardKey k) => KeyDownEvent(
          physicalKey: PhysicalKeyboardKey.keyA,
          logicalKey: k,
          timeStamp: Duration.zero,
        );

    const zoomKeys = [
      LogicalKeyboardKey.equal,
      LogicalKeyboardKey.minus,
      LogicalKeyboardKey.digit0,
      LogicalKeyboardKey.numpadAdd,
      LogicalKeyboardKey.numpadSubtract,
      LogicalKeyboardKey.numpad0,
    ];

    // The bypass's real effect (the browser handling its own zoom because
    // Flutter does not preventDefault) is only observable in a real browser, so
    // the platform/modifier logic is verified against the pure predicate and the
    // shortcut map directly rather than through widget behavior.

    testWidgets('isBrowserZoomKey: macOS zooms with Cmd, not Ctrl/Alt', (
      tester,
    ) async {
      await tester.pumpWidget(const SizedBox());
      debugDefaultTargetPlatformOverride = TargetPlatform.macOS;

      await tester.sendKeyDownEvent(LogicalKeyboardKey.meta);
      for (final k in zoomKeys) {
        expect(GhosttyTerminalState.isBrowserZoomKey(zoomKey(k)), isTrue,
            reason: 'Cmd+$k is a browser-zoom combo on macOS');
      }
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.pageUp)),
          isFalse,
          reason: 'PageUp is not a zoom key');

      // A conflicting Ctrl, or Alt, disqualifies the combo.
      await tester.sendKeyDownEvent(LogicalKeyboardKey.control);
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.equal)),
          isFalse,
          reason: 'Ctrl is a conflicting modifier on macOS');
      await tester.sendKeyUpEvent(LogicalKeyboardKey.control);

      await tester.sendKeyDownEvent(LogicalKeyboardKey.alt);
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.equal)),
          isFalse,
          reason: 'Alt disqualifies the zoom combo');
      await tester.sendKeyUpEvent(LogicalKeyboardKey.alt);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.meta);

      // No modifier held -> never a zoom key.
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.equal)),
          isFalse);
      // Clear before the test body returns: the framework asserts foundation
      // debug vars are unset at end-of-body (before tearDown runs).
      debugDefaultTargetPlatformOverride = null;
    });

    testWidgets('isBrowserZoomKey: Linux/Windows zoom with Ctrl, not Cmd', (
      tester,
    ) async {
      await tester.pumpWidget(const SizedBox());
      debugDefaultTargetPlatformOverride = TargetPlatform.linux;

      await tester.sendKeyDownEvent(LogicalKeyboardKey.control);
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.equal)),
          isTrue,
          reason: 'Ctrl+= is the browser-zoom combo on Linux/Windows');
      await tester.sendKeyUpEvent(LogicalKeyboardKey.control);

      await tester.sendKeyDownEvent(LogicalKeyboardKey.meta);
      expect(
          GhosttyTerminalState.isBrowserZoomKey(
              zoomKey(LogicalKeyboardKey.equal)),
          isFalse,
          reason: 'Cmd is not the Linux zoom modifier');
      await tester.sendKeyUpEvent(LogicalKeyboardKey.meta);
      debugDefaultTargetPlatformOverride = null;
    });

    test('zoomShortcutsFor binds Cmd on macOS and Ctrl elsewhere', () {
      for (final a in zoomShortcutsFor(TargetPlatform.macOS)
          .keys
          .cast<SingleActivator>()) {
        expect(a.meta, isTrue);
        expect(a.control, isFalse);
      }
      for (final a in zoomShortcutsFor(TargetPlatform.linux)
          .keys
          .cast<SingleActivator>()) {
        expect(a.control, isTrue);
        expect(a.meta, isFalse);
      }
    });
  });

  group('focus on open', () {
    testWidgets('re-applies focus once the font loads if requested early', (
      tester,
    ) async {
      // Hold the font load pending so the terminal stays a placeholder and its
      // FocusNode isn't attached yet — mirroring the real "focus requested from
      // ide_layout.initState before the terminal mounts" timing.
      final fontGate = Completer<ByteData>();
      final realFont =
          await rootBundle.load('assets/fonts/JetBrainsMono-Regular.ttf');
      GhosttyTerminalState.loadFontAsset = (_) => fontGate.future;
      addTearDown(() => GhosttyTerminalState.loadFontAsset = rootBundle.load);

      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key));

      // Request focus while the placeholder is shown (node not attached yet).
      key.currentState!.requestFocus();
      await tester.pump();

      // Font loads -> the real TerminalView mounts -> focus is re-applied.
      fontGate.complete(realFont);
      await tester.pumpAndSettle();

      expect(key.currentState!.hasFocus, isTrue,
          reason: 'focus requested before mount must be re-applied on load');
      client.close();
    });
  });
}
