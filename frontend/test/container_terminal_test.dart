import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/terminal/container_terminal.dart';
import 'package:bark_frontend/utils/backend_url.dart';

class _MockAguiClient extends AguiClient {
  final StreamController<AguiEvent> _controller =
      StreamController<AguiEvent>.broadcast();
  final StreamController<String> _terminalController =
      StreamController<String>.broadcast();

  @override
  Stream<AguiEvent> get events => _controller.stream;

  @override
  Stream<String> get terminalOutput => _terminalController.stream;

  void emit(AguiEvent event) => _controller.add(event);

  void emitTerminal(String data) => _terminalController.add(data);

  void close() {
    _controller.close();
    _terminalController.close();
  }
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('ContainerTerminal', () {
    testWidgets('renders terminal widget', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ContainerTerminal(aguiClient: client),
          ),
        ),
      );

      expect(find.byType(ContainerTerminal), findsOneWidget);
      client.close();
    });

    testWidgets('has a requestFocus method via key', (tester) async {
      final client = _MockAguiClient();
      final key = GlobalKey<ContainerTerminalState>();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ContainerTerminal(key: key, aguiClient: client),
          ),
        ),
      );

      // requestFocus should not throw
      key.currentState!.requestFocus();
      await tester.pump();
      expect(find.byType(ContainerTerminal), findsOneWidget);
      client.close();
    });
  });
}
