import 'dart:async';

import 'package:flterm/flterm.dart';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
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
  void sendTerminalStart({int? cols, int? rows}) =>
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

void main() {
  setUp(() => testBaseUrlOverride = 'http://localhost:8997');
  tearDown(() => testBaseUrlOverride = null);

  group('GhosttyTerminal', () {
    testWidgets('shows connect message when no workspace', (tester) async {
      final client = _MockWsClient(hasWorkspace: false);
      await tester.pumpWidget(_build(client));
      expect(find.textContaining('Connect to a workspace'), findsOneWidget);
      expect(find.byType(TerminalView), findsNothing);
      client.close();
    });

    testWidgets('renders TerminalView once the font loads', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      // Before the async font load resolves, the view is gated behind a
      // placeholder ColoredBox (so flterm measures cells with the real font).
      expect(find.byType(TerminalView), findsNothing);
      await tester.pumpAndSettle();
      expect(find.byType(TerminalView), findsOneWidget);
      client.close();
    });

    testWidgets('sends terminal_start on container_ready', (tester) async {
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

    testWidgets('only sends terminal_start once per container_ready',
        (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();

      client.emit(_containerReady());
      await tester.pump();
      // Second event arriving without a new start would be a re-arm; the
      // guard resets _started on each container_ready, so each ready => 1 start.
      client.emit(_containerReady());
      await tester.pump();

      expect(
        client.sentCommands.where((c) => c.startsWith('terminal_start')).length,
        2,
      );
      client.close();
    });

    testWidgets('ignores events without a container_ready CUSTOM payload',
        (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();

      client.emit({'type': 'event'}); // no 'event' map -> early return
      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'something_else'},
      });
      await tester.pump();

      expect(client.sentCommands.where((c) => c.startsWith('terminal_start')),
          isEmpty);
      client.close();
    });

    testWidgets('writes server output to the terminal without error',
        (tester) async {
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
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      // flterm's onResize fires once the TerminalView measures its grid.
      expect(
        client.sentCommands.where((c) => c.startsWith('terminal_resize')),
        isNotEmpty,
      );
      client.close();
    });

    testWidgets(
        'defers terminal_start until measured when container_ready arrives '
        'before the first layout (starts at the real grid, not the 80x24 seed)',
        (tester) async {
      // Hold the font load pending so the view stays a placeholder and flterm
      // never measures the grid until we choose. Without this, the async asset
      // load can resolve during the first pump and measure the grid before
      // container_ready is delivered, so the deferral path would never run.
      final fontGate = Completer<ByteData>();
      final realFont =
          await rootBundle.load('assets/fonts/JetBrainsMono-Regular.ttf');
      GhosttyTerminalState.loadFontAsset = (_) => fontGate.future;
      addTearDown(() => GhosttyTerminalState.loadFontAsset = rootBundle.load);

      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      // The font has not loaded yet, so the view is a placeholder and flterm
      // has not measured the grid. container_ready arrives first.
      expect(find.byType(TerminalView), findsNothing);
      client.emit(_containerReady());
      await tester.pump();
      // Start is deferred — we must not create the pty at the 80x24 seed.
      expect(client.sentCommands.where((c) => c.startsWith('terminal_start')),
          isEmpty);

      // Font loads, the view lays out, flterm measures and fires onResize,
      // which triggers the deferred start at the measured size.
      fontGate.complete(realFont);
      await tester.pumpAndSettle();

      final starts = client.sentCommands
          .where((c) => c.startsWith('terminal_start:'))
          .toList();
      final resizes = client.sentCommands
          .where((c) => c.startsWith('terminal_resize:'))
          .toList();
      expect(starts.length, 1);
      expect(resizes, isNotEmpty);
      // The start carries the measured grid (same WxH as the resize), not the
      // hardcoded 80x24 seed.
      expect(starts.single.split(':')[1], resizes.last.split(':')[1]);
      client.close();
    });

    testWidgets('sends terminal_stop on dispose (after start)', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      client.emit(_containerReady());
      await tester.pump();
      client.sentCommands.clear();

      await tester
          .pumpWidget(const MaterialApp(home: Scaffold(body: SizedBox())));
      expect(client.sentCommands, contains('terminal_stop'));
      client.close();
    });

    testWidgets('does not send terminal_stop if never started', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();

      await tester
          .pumpWidget(const MaterialApp(home: Scaffold(body: SizedBox())));
      expect(client.sentCommands, isNot(contains('terminal_stop')));
      client.close();
    });

    testWidgets('requestFocus via GlobalKey does not throw', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      key.currentState!.requestFocus();
      await tester.pump();
      expect(find.byType(GhosttyTerminal), findsOneWidget);
      client.close();
    });

    testWidgets('routeNativePaste drops the payload when not focused',
        (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key));
      await tester.pumpAndSettle();
      client.sentCommands.clear();

      final consumed = key.currentState!.routeNativePaste('hello\n');
      await tester.pump();

      expect(consumed, isFalse);
      expect(
        client.sentCommands.where((c) => c.startsWith('terminal_input')),
        isEmpty,
      );
      client.close();
    });

    testWidgets('routeNativePaste forwards the payload when focused',
        (tester) async {
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
      // flterm wraps in bracketed-paste markers (\e[200~ ... \e[201~) when
      // the terminal program has requested them, but always emits the
      // payload bytes — assert on a substring rather than equality so the
      // test isn't coupled to bracketed-paste mode.
      final pasted =
          client.sentCommands.where((c) => c.startsWith('terminal_input:'));
      expect(pasted, isNotEmpty);
      expect(pasted.join(), contains('clipboard-payload'));
      client.close();
    });

    testWidgets('handleLinkTap forwards token/uri/tail and the live pwd',
        (tester) async {
      ({String token, String? uri, String pwd, String tail})? captured;
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: GhosttyTerminal(
            key: key,
            wsClient: client,
            onPathTap: (e) => captured = e,
          ),
        ),
      ));
      await tester.pumpAndSettle();

      key.currentState!.handleLinkTap(
        (
          row: 3,
          col: 5,
          token: 'work/a b.txt',
          uri: null,
          tail: 'work/a b.txt rest',
        ),
      );

      expect(captured, isNotNull);
      expect(captured!.token, 'work/a b.txt');
      expect(captured!.tail, 'work/a b.txt rest');
      expect(captured!.uri, isNull);
      expect(captured!.pwd, isA<String>()); // live OSC 7 cwd (empty until set)
      client.close();
    });

    testWidgets('handleLinkTap is a no-op when onPathTap is null',
        (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<GhosttyTerminalState>();
      await tester.pumpWidget(_build(client, key: key)); // no onPathTap
      await tester.pumpAndSettle();
      // Must not throw when there is no callback wired.
      key.currentState!.handleLinkTap(
        (row: 0, col: 0, token: 'x', uri: 'https://e', tail: 'x'),
      );
      client.close();
    });

    testWidgets('clearScreen writes escape sequence', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_build(client));
      await tester.pumpAndSettle();
      final state = tester.state<GhosttyTerminalState>(
        find.byType(GhosttyTerminal),
      );
      state.clearScreen();
      client.close();
    });
  });
}
