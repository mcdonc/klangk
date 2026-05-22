import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
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

  final List<String> sentPrompts = [];

  void emitError(String error) => _errorController.add(error);

  @override
  void sendPrompt(String text) => sentPrompts.add(text);

  @override
  void sendAbort() {}

  void close() {
    _controller.close();
    _errorController.close();
  }
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    testChatHttpClientOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testChatHttpClientOverride = null;
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

    testWidgets('send prompt via send button', (tester) async {
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
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'button prompt');
      await tester.tap(find.byIcon(Icons.send));
      await tester.pumpAndSettle();

      expect(client.sentPrompts, contains('button prompt'));
      client.close();
    });

    testWidgets('tool call output delta updates entry', (tester) async {
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
      await tester.pumpAndSettle();

      // Start a tool call
      client.emit(AguiEvent(
        type: AguiEventType.toolCallStart,
        data: {
          'toolCallId': 't1',
          'toolCallName': 'bash',
          'toolCallArgs': 'echo hi',
        },
      ));
      await tester.pump();

      // Send output delta
      client.emit(AguiEvent(
        type: AguiEventType.toolCallEnd,
        data: {'toolCallId': 't1', 'delta': 'hello output'},
      ));
      await tester.pump();

      expect(find.byType(ChatPanel), findsOneWidget);
      client.close();
    });

    testWidgets('loads message history on init', (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/messages')) {
          return http.Response(
            jsonEncode([
              {'entry_type': 'user', 'content': 'hello'},
              {'entry_type': 'assistant', 'content': 'hi there'},
              {
                'entry_type': 'tool_call',
                'content': '',
                'tool_name': 'bash',
                'tool_args': 'ls',
                'tool_output': 'file.txt',
                'is_complete': true,
              },
              {'entry_type': 'error', 'content': 'oops'},
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

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
      await tester.pumpAndSettle();

      expect(find.text('hello'), findsOneWidget);
      expect(find.textContaining('hi there'), findsOneWidget);
      client.close();
    });

    testWidgets('loads queued message from history', (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/messages')) {
          return http.Response(
            jsonEncode([
              {
                'entry_type': 'user',
                'content': 'queued msg',
                'is_queued': true,
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

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
      await tester.pumpAndSettle();

      expect(find.text('queued msg'), findsOneWidget);
      expect(find.text('queued'), findsOneWidget);
      client.close();
    });

    testWidgets('send prompt via enter key adds user message', (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        return http.Response(jsonEncode([]), 200);
      });

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
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'test prompt');
      await tester.tap(find.byIcon(Icons.send));
      await tester.pumpAndSettle();

      expect(client.sentPrompts, contains('test prompt'));
      expect(find.text('test prompt'), findsOneWidget);
      client.close();
    });

    testWidgets('prompt_queued event marks last user entry as queued',
        (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        return http.Response(jsonEncode([]), 200);
      });

      final client = _MockAguiClient();
      final key = GlobalKey<ChatPanelState>();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              key: key,
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Send a prompt via state
      await tester.enterText(find.byType(TextField), 'my prompt');
      key.currentState!.sendPromptFromUI();
      await tester.pumpAndSettle();

      expect(find.text('my prompt'), findsOneWidget);

      // Emit prompt_queued custom event
      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {'name': 'prompt_queued'},
      ));
      await tester.pumpAndSettle();

      expect(find.text('queued'), findsOneWidget);
      client.close();
    });

    testWidgets('arrow up/down navigates history', (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        return http.Response(jsonEncode([]), 200);
      });

      final client = _MockAguiClient();
      final key = GlobalKey<ChatPanelState>();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: ChatPanel(
              key: key,
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Send two prompts via the state
      await tester.enterText(find.byType(TextField), 'first');
      key.currentState!.sendPromptFromUI();
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'second');
      key.currentState!.sendPromptFromUI();
      await tester.pumpAndSettle();

      // Navigate history directly via the state
      key.currentState!.navigateHistory(-1); // up
      await tester.pump();
      var tf = tester.widget<TextField>(find.byType(TextField));
      expect(tf.controller!.text, 'second');

      key.currentState!.navigateHistory(-1); // up again
      await tester.pump();
      tf = tester.widget<TextField>(find.byType(TextField));
      expect(tf.controller!.text, 'first');

      key.currentState!.navigateHistory(1); // down
      await tester.pump();
      tf = tester.widget<TextField>(find.byType(TextField));
      expect(tf.controller!.text, 'second');

      key.currentState!.navigateHistory(1); // down to saved
      await tester.pump();
      tf = tester.widget<TextField>(find.byType(TextField));
      expect(tf.controller!.text, '');

      client.close();
    });

    testWidgets('toolCallArgs delta appends to tool output', (tester) async {
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

      // Start tool call
      client.emit(AguiEvent(
        type: AguiEventType.toolCallStart,
        data: {
          'toolCallId': 't1',
          'toolCallName': 'bash',
          'toolCallArgs': 'echo hi',
        },
      ));
      await tester.pump();

      // Send toolCallArgs delta
      client.emit(AguiEvent(
        type: AguiEventType.toolCallArgs,
        data: {'toolCallId': 't1', 'delta': 'output line'},
      ));
      await tester.pump();

      expect(find.byType(ChatPanel), findsOneWidget);
      client.close();
    });

    testWidgets('tool call end with delta appends to output', (tester) async {
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
      await tester.pumpAndSettle();

      // Start tool call
      client.emit(AguiEvent(
        type: AguiEventType.toolCallStart,
        data: {
          'toolCallId': 't1',
          'toolCallName': 'bash',
          'toolCallArgs': 'echo hi',
        },
      ));
      await tester.pump();

      // Send delta via TOOL_CALL_END with delta
      client.emit(AguiEvent(
        type: AguiEventType.toolCallEnd,
        data: {'toolCallId': 't1', 'delta': 'line1\n'},
      ));
      await tester.pump();

      // Send more delta
      client.emit(AguiEvent(
        type: AguiEventType.toolCallEnd,
        data: {'toolCallId': 't1', 'delta': 'line2\n'},
      ));
      await tester.pump();

      expect(find.byType(ChatPanel), findsOneWidget);
      client.close();
    });

    testWidgets('long tool args are truncated in subtitle', (tester) async {
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
          'toolCallId': 't1',
          'toolCallName': 'bash',
          'toolCallArgs': 'x' * 100,
        },
      ));
      await tester.pump();

      // Expand the tool call tile to see the subtitle
      await tester.tap(find.textContaining('bash'));
      await tester.pump();

      expect(find.textContaining('${'x' * 80}...'), findsOneWidget);
      client.close();
    });

    testWidgets('long tool output is truncated', (tester) async {
      testChatHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/messages')) {
          return http.Response(
            jsonEncode([
              {
                'entry_type': 'tool_call',
                'content': 'bash',
                'tool_name': 'bash',
                'tool_args': 'big cmd',
                'tool_output': 'y' * 3000,
                'is_complete': true,
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

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
      await tester.pumpAndSettle();

      // Expand the tool call tile
      await tester.tap(find.textContaining('bash'));
      await tester.pump();

      expect(find.textContaining('${'y' * 2000}...'), findsOneWidget);
      client.close();
    });
  });
}
