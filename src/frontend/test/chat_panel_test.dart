import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/terminal/chat_panel.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

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

    testWidgets('shows copy button next to links in assistant message',
        (tester) async {
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
        data: {
          'messageId': 'm1',
          'delta': 'Visit http://localhost:8995/hosted/abc/9000/ for the app'
        },
      ));
      await tester.pump();

      // The URL should be rendered as a link with a copy icon
      expect(find.byIcon(Icons.copy), findsOneWidget);
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

      expect(find.byType(ChatPanel), findsOneWidget);
      client.close();
    });

    testWidgets('completes message on TEXT_MESSAGE_END', (tester) async {
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
      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm1', 'delta': 'Complete message'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.textMessageEnd,
        data: {'messageId': 'm1'},
      ));
      await tester.pump();

      expect(find.textContaining('Complete message'), findsOneWidget);
      client.close();
    });

    testWidgets('shows tool call with result', (tester) async {
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
          'toolCallName': 'write',
          'toolCallArgs': 'path=hello.txt',
        },
      ));
      await tester.pump();

      client.emit(AguiEvent(
        type: AguiEventType.toolCallEnd,
        data: {'toolCallId': 'tc-1'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.toolCallResult,
        data: {'toolCallId': 'tc-1', 'content': 'File written'},
      ));
      await tester.pump();

      expect(find.textContaining('write'), findsOneWidget);
      client.close();
    });

    testWidgets('hides abort button after run finishes', (tester) async {
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

      // Start a run
      client.emit(AguiEvent(
        type: AguiEventType.runStarted,
        data: {'threadId': 'ws-1'},
      ));
      await tester.pump();
      expect(find.byIcon(Icons.stop_circle), findsOneWidget);

      // Finish the run
      client.emit(AguiEvent(
        type: AguiEventType.runFinished,
        data: {'threadId': 'ws-1'},
      ));
      await tester.pump();
      expect(find.byIcon(Icons.stop_circle), findsNothing);
      expect(find.byIcon(Icons.send), findsOneWidget);
      client.close();
    });

    testWidgets('shows multiple messages in sequence', (tester) async {
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

      // First message
      client.emit(AguiEvent(
        type: AguiEventType.textMessageStart,
        data: {'messageId': 'm1'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm1', 'delta': 'First response'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.textMessageEnd,
        data: {'messageId': 'm1'},
      ));
      await tester.pump();

      // Second message
      client.emit(AguiEvent(
        type: AguiEventType.textMessageStart,
        data: {'messageId': 'm2'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm2', 'delta': 'Second response'},
      ));
      client.emit(AguiEvent(
        type: AguiEventType.textMessageEnd,
        data: {'messageId': 'm2'},
      ));
      await tester.pump();

      expect(find.textContaining('First response'), findsOneWidget);
      expect(find.textContaining('Second response'), findsOneWidget);
      client.close();
    });

    testWidgets('shows run error', (tester) async {
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
        type: AguiEventType.runError,
        data: {'message': 'Something went wrong'},
      ));
      await tester.pump();

      expect(find.textContaining('Something went wrong'), findsOneWidget);
      client.close();
    });

    testWidgets('accumulates streaming deltas', (tester) async {
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
      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm1', 'delta': 'Hello '},
      ));
      await tester.pump();

      client.emit(AguiEvent(
        type: AguiEventType.textMessageContent,
        data: {'messageId': 'm1', 'delta': 'World'},
      ));
      await tester.pump();

      expect(find.textContaining('Hello World'), findsOneWidget);
      client.close();
    });
  });
}
