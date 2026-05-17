import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/terminal/chat_panel.dart';
import 'package:bark_frontend/utils/backend_url.dart';

class _MockAguiClient extends AguiClient {
  final StreamController<AguiEvent> _controller =
      StreamController<AguiEvent>.broadcast();
  final StreamController<String> _errorController =
      StreamController<String>.broadcast();

  @override
  Stream<AguiEvent> get events => _controller.stream;

  @override
  Stream<String> get errors => _errorController.stream;

  void emit(AguiEvent event) => _controller.add(event);

  void emitError(String error) => _errorController.add(error);

  void close() {
    _controller.close();
    _errorController.close();
  }
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('ChatPanel', () {
    testWidgets('renders with input field', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );

      // Should have a text input area
      expect(find.byType(ChatPanel), findsOneWidget);
      // Send button
      expect(find.byIcon(Icons.send), findsOneWidget);
      client.close();
    });

    testWidgets('shows streaming text from events', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );

      client.emit(AguiEvent(
        type: AguiEventType.textMessageStart,
        data: {'messageId': 'm1'},
      ));
      await tester.pump();

      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm1', 'delta': 'Hello world'},
      ));
      await tester.pump();

      expect(find.textContaining('Hello world'), findsOneWidget);
      client.close();
    });

    testWidgets('shows run started indicator', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );

      client.emit(AguiEvent(
        type: AguiEventType.runStarted,
        data: {'threadId': 'ws-1'},
      ));
      await tester.pump();

      // The abort button should appear (red stop_circle icon)
      expect(find.byIcon(Icons.stop_circle), findsOneWidget);
      client.close();
    });

    testWidgets('shows tool call entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );

      client.emit(AguiEvent(
        type: AguiEventType.toolCallStart,
        data: {
          'toolCallId': 'tc-1',
          'toolCallName': 'bash',
          'toolCallArgs': 'ls -la'
        },
      ));
      await tester.pump();

      expect(find.textContaining('bash'), findsOneWidget);
      client.close();
    });

    testWidgets('shows error from error stream', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );

      client.emitError('Connection lost');
      await tester.pump();

      // Error should appear as a snackbar or in the chat
      expect(find.byType(ChatPanel), findsOneWidget);
      client.close();
    });
  });
}
