import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/output/output_panel.dart';
import 'package:bark_frontend/utils/backend_url.dart';

/// Minimal mock of AguiClient that just provides an event stream.
class _MockAguiClient extends AguiClient {
  final StreamController<AguiEvent> _controller =
      StreamController<AguiEvent>.broadcast();

  @override
  Stream<AguiEvent> get events => _controller.stream;

  void emit(AguiEvent event) => _controller.add(event);

  void close() => _controller.close();
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('OutputPanel', () {
    testWidgets('shows "No output yet" initially', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      expect(find.text('No output yet'), findsOneWidget);
      expect(find.text('Debug'), findsOneWidget);
      client.close();
    });

    testWidgets('shows tool call entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.toolCallStart,
        data: {'toolCallName': 'bash', 'toolCallArgs': 'ls'},
      ));
      await tester.pump();

      expect(find.text('bash'), findsOneWidget);
    });

    testWidgets('shows tool result entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.toolCallResult,
        data: {'content': 'file.txt'},
      ));
      await tester.pump();

      expect(find.text('Result'), findsOneWidget);
      expect(find.text('file.txt'), findsOneWidget);
    });

    testWidgets('shows error entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.runError,
        data: {'message': 'something broke'},
      ));
      await tester.pump();

      expect(find.text('Error'), findsOneWidget);
      expect(find.text('something broke'), findsOneWidget);
    });

    testWidgets('shows reasoning entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.reasoningMessageContent,
        data: {'delta': 'thinking...'},
      ));
      await tester.pump();

      expect(find.text('Thinking'), findsOneWidget);
      expect(find.text('thinking...'), findsOneWidget);
    });

    testWidgets('appends to existing reasoning entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.reasoningMessageContent,
        data: {'delta': 'first '},
      ));
      await tester.pump();

      client.emit(AguiEvent(
        type: AguiEventType.reasoningMessageContent,
        data: {'delta': 'second'},
      ));
      await tester.pump();

      expect(find.text('first second'), findsOneWidget);
      // Only one "Thinking" header
      expect(find.text('Thinking'), findsOneWidget);
    });

    testWidgets('shows step entry', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.stepStarted,
        data: {'stepName': 'turn'},
      ));
      await tester.pump();

      expect(find.text('turn'), findsOneWidget);
      expect(find.text('Started'), findsOneWidget);
    });

    testWidgets('shows custom query_prompt event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'query_prompt',
          'value': {'text': 'hello world'},
        },
      ));
      await tester.pump();

      expect(find.text('query'), findsOneWidget);
      expect(find.text('hello world'), findsOneWidget);
    });

    testWidgets('shows container_ready event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'container_ready',
          'value': {'reason': 'Created new container'},
        },
      ));
      await tester.pump();

      expect(find.text('Container Ready'), findsOneWidget);
    });

    testWidgets('shows container_stopped event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'container_stopped',
          'value': {'reason': 'idle timeout'},
        },
      ));
      await tester.pump();

      expect(find.text('Container Stopped'), findsOneWidget);
    });

    testWidgets('shows container_restart event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'container_restart',
          'value': {'reason': 'Restarting...'},
        },
      ));
      await tester.pump();

      expect(find.text('Container Restart'), findsOneWidget);
    });

    testWidgets('shows container_starting event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'container_starting',
          'value': {'reason': 'Starting...'},
        },
      ));
      await tester.pump();

      expect(find.text('Container Starting'), findsOneWidget);
    });

    testWidgets('shows session_resume event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'session_resume',
          'value': {'reason': 'Resuming session'},
        },
      ));
      await tester.pump();

      expect(find.text('Session Resume'), findsOneWidget);
    });

    testWidgets('shows extension_ui_request event', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {
          'name': 'extension_ui_request',
          'value': {'method': 'input', 'title': 'HOST_TOOL_REQUEST'},
        },
      ));
      await tester.pump();

      expect(find.text('Extension UI: input'), findsOneWidget);
    });

    testWidgets('clear button removes entries', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.runError,
        data: {'message': 'error'},
      ));
      await tester.pump();
      expect(find.text('error'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.clear_all));
      await tester.pump();
      expect(find.text('No output yet'), findsOneWidget);
    });

    testWidgets('ignores empty reasoning delta', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: OutputPanel(aguiClient: client))),
      );

      client.emit(AguiEvent(
        type: AguiEventType.reasoningMessageContent,
        data: {'delta': ''},
      ));
      await tester.pump();

      expect(find.text('No output yet'), findsOneWidget);
    });
  });
}
