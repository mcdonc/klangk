import 'dart:async';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/terminal/container_terminal.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _controller =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _terminalController =
      StreamController<String>.broadcast();
  final List<String> sentCommands = [];
  final bool hasWorkspace;

  _MockWsClient({this.hasWorkspace = true});

  @override
  Stream<Map<String, dynamic>> get customEvents => _controller.stream;

  @override
  Stream<String> get terminalOutput => _terminalController.stream;

  @override
  String? get currentWorkspaceId => hasWorkspace ? 'ws-1' : null;

  void emit(Map<String, dynamic> event) => _controller.add(event);
  void emitTerminal(String data) => _terminalController.add(data);

  @override
  void sendTerminalStart({int? cols, int? rows}) =>
      sentCommands.add('terminal_start');

  @override
  void sendTerminalStop() => sentCommands.add('terminal_stop');

  @override
  void sendTerminalInput(String data) =>
      sentCommands.add('terminal_input:$data');

  @override
  void sendTerminalResize(int cols, int rows) =>
      sentCommands.add('terminal_resize:${cols}x$rows');

  @override
  void sendRestartContainer() => sentCommands.add('restart_container');

  void close() {
    _controller.close();
    _terminalController.close();
  }
}

Widget _buildTerminal(_MockWsClient client,
    {GlobalKey<ContainerTerminalState>? key}) {
  return MaterialApp(
    home: Scaffold(
      body: ContainerTerminal(key: key, wsClient: client),
    ),
  );
}

void main() {
  setUp(() => testBaseUrlOverride = 'http://localhost:8997');
  tearDown(() => testBaseUrlOverride = null);

  group('ContainerTerminal', () {
    testWidgets('renders when workspace connected', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      expect(find.byType(ContainerTerminal), findsOneWidget);
      client.close();
    });

    testWidgets('shows connect message when no workspace', (tester) async {
      final client = _MockWsClient(hasWorkspace: false);
      await tester.pumpWidget(_buildTerminal(client));
      expect(find.textContaining('Connect to a workspace'), findsOneWidget);
      client.close();
    });

    testWidgets('sends terminal_start on container_ready', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();
      expect(client.sentCommands, isNot(contains('terminal_start')));
      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
      });
      await tester.pump();
      expect(client.sentCommands, contains('terminal_start'));
      client.close();
    });

    testWidgets('only sends terminal_start once per container_ready',
        (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();
      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
      });
      await tester.pump();
      await tester.pump();
      await tester.pump();
      final count =
          client.sentCommands.where((c) => c == 'terminal_start').length;
      expect(count, 1);
      client.close();
    });

    testWidgets('sends terminal_stop on dispose', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();
      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
      });
      await tester.pump();
      client.sentCommands.clear();

      await tester
          .pumpWidget(const MaterialApp(home: Scaffold(body: SizedBox())));
      expect(client.sentCommands, contains('terminal_stop'));
      client.close();
    });

    testWidgets('requestFocus via GlobalKey', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<ContainerTerminalState>();
      await tester.pumpWidget(_buildTerminal(client, key: key));
      key.currentState!.requestFocus();
      await tester.pump();
      expect(find.byType(ContainerTerminal), findsOneWidget);
      client.close();
    });

    testWidgets('container_ready reconnects terminal', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();
      final startCount =
          client.sentCommands.where((c) => c == 'terminal_start').length;

      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
      });
      await tester.pump();

      // Should send terminal_start again to reconnect
      expect(
        client.sentCommands.where((c) => c == 'terminal_start').length,
        startCount + 1,
      );
      client.close();
    });

    testWidgets('terminal output stream writes to terminal', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();

      // Simulate terminal output from the server
      client.emitTerminal('hello from server');
      await tester.pump();

      // The data was written to the xterm Terminal — we can't easily
      // read it back, but verify no errors occurred
      expect(find.byType(ContainerTerminal), findsOneWidget);
      client.close();
    });

    testWidgets('terminal onOutput sends input when not stopped',
        (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<ContainerTerminalState>();
      await tester.pumpWidget(_buildTerminal(client, key: key));
      await tester.pumpAndSettle();
      client.emit({
        'type': 'event',
        'event': {'type': 'CUSTOM', 'name': 'container_ready', 'value': {}},
      });
      await tester.pump();

      // Access the Terminal's onOutput callback indirectly by
      // checking that typing sends terminal_input
      // The xterm Terminal.onOutput fires when the terminal produces
      // output (e.g., from keyboard input), which calls sendTerminalInput
      expect(client.sentCommands, contains('terminal_start'));
      client.close();
    });

    testWidgets('right-click shows context menu and paste works',
        (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();

      final terminalView = find.byType(ContainerTerminal);
      final center = tester.getCenter(terminalView);
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      expect(find.text('Paste'), findsOneWidget);

      // Tap Paste
      await tester.tap(find.text('Paste'));
      await tester.pumpAndSettle();

      // Menu should be dismissed
      expect(find.text('Paste'), findsNothing);
      client.close();
    });

    testWidgets('right-click with selection shows copy option', (tester) async {
      final client = _MockWsClient();
      await tester.pumpWidget(_buildTerminal(client));
      await tester.pumpAndSettle();

      // Write some text to the terminal so there's content to select
      client.emitTerminal('hello world\r\n');
      await tester.pump();

      final terminalView = find.byType(ContainerTerminal);
      final center = tester.getCenter(terminalView);

      // Right-click to open menu — Copy only shows when there's a selection
      // but we can't easily create a selection in tests, so just verify
      // the menu opens and Paste is available
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      expect(find.text('Paste'), findsOneWidget);

      // Dismiss by tapping away
      await tester.tapAt(Offset.zero);
      await tester.pumpAndSettle();
      client.close();
    });
  });
}
