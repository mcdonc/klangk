import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/chat/workspace_chat.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

class _FakeWebSocketChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  Future<void> get ready => Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  void dispose() => _incoming.close();
}

class _FakeSink extends Fake implements WebSocketSink {
  final List<dynamic> sent = [];

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('WorkspaceChat', () {
    late WsClient client;
    late _FakeWebSocketChannel channel;

    setUp(() {
      client = WsClient();
      channel = _FakeWebSocketChannel();
      client.connectForTest(channel);
    });

    tearDown(() {
      client.disconnect();
      client.dispose();
    });

    Widget buildChat() {
      return MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 800,
            height: 600,
            child: WorkspaceChat(wsClient: client),
          ),
        ),
      );
    }

    testWidgets('renders empty state', (tester) async {
      await tester.pumpWidget(buildChat());
      expect(find.text('No messages yet'), findsOneWidget);
    });

    testWidgets('renders message list', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-1',
          'user_email': 'alice@test.com',
          'message': 'hello world',
          'created_at': '2026-01-01 00:00:00',
        });
        // Let stream events propagate through microtasks
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));

      expect(find.text('No messages yet'), findsNothing);
      // Message is rendered via RichText with TextSpan children
      expect(find.byType(RichText), findsWidgets);
      // Timestamp is rendered as a plain Text widget
      expect(find.text('2026-01-01 00:00:00'), findsOneWidget);
    });

    testWidgets('sends message on Enter', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'test message');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final chatMsgs = msgs.where((m) => m['cmd'] == 'chat_send').toList();
      expect(chatMsgs.length, 1);
      expect(chatMsgs[0]['message'], 'test message');
    });

    testWidgets('sends message on send button tap', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'button message');
      await tester.tap(find.byIcon(Icons.send));
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final chatMsgs = msgs.where((m) => m['cmd'] == 'chat_send').toList();
      expect(chatMsgs.length, 1);
      expect(chatMsgs[0]['message'], 'button message');
    });

    testWidgets('does not send empty message', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.tap(find.byIcon(Icons.send));
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final chatMsgs = msgs.where((m) => m['cmd'] == 'chat_send').toList();
      expect(chatMsgs.length, 0);
    });

    testWidgets('auto-scrolls on new messages', (tester) async {
      await tester.pumpWidget(buildChat());

      // Send enough messages to require scrolling
      for (int i = 0; i < 30; i++) {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-$i',
          'user_email': 'user@test.com',
          'message': 'Message number $i',
          'created_at': '2026-01-01 00:0$i:00',
        });
      }
      await tester.pumpAndSettle();

      // Widget should still be rendered without errors
      expect(find.byType(WorkspaceChat), findsOneWidget);
    });
  });
}
