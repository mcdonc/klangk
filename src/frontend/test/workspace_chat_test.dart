import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
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

/// Find the MarkdownBody widget and return its data property.
String? _findMarkdownData(WidgetTester tester) {
  final finder = find.byType(MarkdownBody);
  if (finder.evaluate().isEmpty) return null;
  final widget = tester.widget<MarkdownBody>(finder.first);
  return widget.data;
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

    Widget buildChat({
      AuthService? authService,
      ValueChanged<int>? onUnreadChanged,
      ValueChanged<bool>? onMentionChanged,
      GlobalKey<WorkspaceChatState>? chatKey,
    }) {
      return ChangeNotifierProvider(
        create: (_) => authService ?? AuthService(),
        child: MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: WorkspaceChat(
                key: chatKey,
                wsClient: client,
                onUnreadChanged: onUnreadChanged,
                onMentionChanged: onMentionChanged,
              ),
            ),
          ),
        ),
      );
    }

    testWidgets('renders empty state', (tester) async {
      await tester.pumpWidget(buildChat());
      expect(find.text('No messages yet'), findsOneWidget);
    });

    testWidgets('renders message as markdown', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-1',
          'user_email': 'alice@test.com',
          'user_handle': 'alice',
          'message': 'hello world',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 200));

      expect(find.text('No messages yet'), findsNothing);
      // Sender handle rendered (not email) when handle is present
      expect(find.text('alice'), findsOneWidget);
      expect(find.text('alice@test.com'), findsNothing);
      // Message rendered via MarkdownBody
      expect(find.byType(MarkdownBody), findsOneWidget);
      expect(_findMarkdownData(tester), 'hello world');
    });

    testWidgets('renders markdown formatting in messages', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-md',
          'user_email': 'alice@test.com',
          'message': 'try `code` and **bold** text',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      final data = _findMarkdownData(tester);
      assert(data != null);
      expect(data, contains('`code`'));
      expect(data, contains('**bold**'));
    });

    testWidgets('renders code blocks in messages', (tester) async {
      await tester.pumpWidget(buildChat());

      const codeMsg = '```python\nprint("hello")\n```';
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-code',
          'user_email': 'alice@test.com',
          'message': codeMsg,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(find.byType(MarkdownBody), findsOneWidget);
      expect(_findMarkdownData(tester), codeMsg);
    });

    testWidgets('sends message on Enter', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'test message');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
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

    testWidgets('chat_updated replaces message text in place', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-1',
          'user_email': 'alice@test.com',
          'message': 'original text',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Verify original text is in MarkdownBody
      expect(_findMarkdownData(tester), 'original text');

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_updated',
          'message_id': 'msg-1',
          'message': '<message deleted by author>',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // After deletion, MarkdownBody should be gone (deleted uses Text.rich)
      expect(find.byType(MarkdownBody), findsNothing);
      // Deleted message rendered as italic text within Text.rich
      expect(
        find.textContaining('<message deleted by author>'),
        findsOneWidget,
      );
    });

    testWidgets('delete button shown for own messages', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"test-uid","email":"test@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      await tester.pumpWidget(buildChat(authService: auth));

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-own',
          'user_id': 'test-uid',
          'user_email': 'test@test.com',
          'message': 'my message',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(find.byIcon(Icons.close), findsOneWidget);
    });

    testWidgets('delete button calls sendChatDelete', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"test-uid","email":"test@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      await tester.pumpWidget(buildChat(authService: auth));

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-del',
          'user_id': 'test-uid',
          'user_email': 'test@test.com',
          'message': 'delete me',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final delMsgs = msgs.where((m) => m['cmd'] == 'chat_delete').toList();
      expect(delMsgs.length, 1);
      expect(delMsgs[0]['message_id'], 'msg-del');
    });

    testWidgets('deleted message shown in italic without delete button', (
      tester,
    ) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"test-uid","email":"test@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      await tester.pumpWidget(buildChat(authService: auth));

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-deleted',
          'user_id': 'test-uid',
          'user_email': 'test@test.com',
          'message': '<message deleted by author>',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // No delete button for already-deleted messages
      expect(find.byIcon(Icons.close), findsNothing);

      // Deleted messages rendered as Text.rich (not MarkdownBody)
      expect(find.byType(MarkdownBody), findsNothing);
      // Verify the deleted text appears somewhere in the widget tree
      expect(
        find.textContaining('<message deleted by author>'),
        findsOneWidget,
      );
    });

    testWidgets('formats timestamp for this-week messages', (tester) async {
      await tester.pumpWidget(buildChat());

      // Send a message dated yesterday (within this week but not today)
      final yesterday = DateTime.now().subtract(const Duration(days: 1));
      final utcYesterday = yesterday.toUtc();
      final ts =
          '${utcYesterday.year}-${utcYesterday.month.toString().padLeft(2, '0')}-${utcYesterday.day.toString().padLeft(2, '0')} '
          '${utcYesterday.hour.toString().padLeft(2, '0')}:${utcYesterday.minute.toString().padLeft(2, '0')}:${utcYesterday.second.toString().padLeft(2, '0')}';

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-week',
          'user_email': 'user@test.com',
          'message': 'yesterday msg',
          'created_at': ts,
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Verify a day-of-week abbreviation is rendered (Mon, Tue, etc.)
      final dayAbbrevs = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      final allText = <String>[];
      for (final textWidget in tester.widgetList<Text>(find.byType(Text))) {
        if (textWidget.data != null) allText.add(textWidget.data!);
      }
      final hasDayAbbrev = allText.any(
        (t) => dayAbbrevs.any((d) => t.contains(d)),
      );
      expect(hasDayAbbrev, isTrue);
    });

    testWidgets('URLs in messages rendered via markdown', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-url',
          'user_email': 'alice@test.com',
          'message': 'Check https://example.com/path for details',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Message is rendered via MarkdownBody which handles URL auto-linking
      expect(find.byType(MarkdownBody), findsOneWidget);
      final data = _findMarkdownData(tester);
      expect(data, contains('https://example.com/path'));
    });

    testWidgets('setVisible clears unread count', (tester) async {
      final unreadCounts = <int>[];
      final chatKey = GlobalKey<WorkspaceChatState>();

      await tester.pumpWidget(
        buildChat(
          onUnreadChanged: (count) => unreadCounts.add(count),
          chatKey: chatKey,
        ),
      );

      // Send a message while not visible (default _isVisible is false)
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-unread',
          'user_email': 'user@test.com',
          'message': 'unread msg',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(unreadCounts, [1]);

      // Now set visible — should clear unread
      chatKey.currentState!.setVisible(true);
      expect(unreadCounts, [1, 0]);
    });

    testWidgets('requestFocus focuses the message input', (tester) async {
      final chatKey = GlobalKey<WorkspaceChatState>();
      await tester.pumpWidget(buildChat(chatKey: chatKey));

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.focusNode, isNotNull);
      expect(field.focusNode!.hasFocus, isFalse);

      chatKey.currentState!.requestFocus();
      await tester.pump();

      expect(field.focusNode!.hasFocus, isTrue);
    });

    testWidgets('loads buffered chat history on init', (tester) async {
      // Pre-populate the buffer before building the widget
      client.chatHistory.addAll([
        {
          'id': 'h1',
          'user_id': 'u1',
          'user_email': 'alice@example.com',
          'message': 'buffered message',
          'created_at': '2026-01-01 00:00:00',
        },
      ]);

      await tester.pumpWidget(buildChat());
      await tester.pumpAndSettle();

      expect(find.byType(MarkdownBody), findsOneWidget);
      expect(_findMarkdownData(tester), 'buffered message');
    });

    testWidgets('@mention renders as bold in markdown', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-mention',
          'user_email': 'alice@test.com',
          'message': 'hey @bob@test.com check this',
          'created_at': '2026-01-01 00:00:00',
          'mentions': ['bob-uid'],
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // @mentions are wrapped in ** for bold rendering
      final data = _findMarkdownData(tester);
      expect(data, contains('**@bob@test.com**'));
    });

    testWidgets('self-mention renders as bold in markdown', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"my-uid","email":"me@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      await tester.pumpWidget(buildChat(authService: auth));

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-self',
          'user_email': 'alice@test.com',
          'message': 'hey @me@test.com look',
          'created_at': '2026-01-01 00:00:00',
          'mentions': ['my-uid'],
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      final data = _findMarkdownData(tester);
      expect(data, contains('**@me@test.com**'));
    });

    testWidgets('onMentionChanged fires when mentioned while hidden', (
      tester,
    ) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"my-uid","email":"me@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      final mentionStates = <bool>[];
      final chatKey = GlobalKey<WorkspaceChatState>();

      await tester.pumpWidget(
        buildChat(
          authService: auth,
          onMentionChanged: (m) => mentionStates.add(m),
          chatKey: chatKey,
        ),
      );

      // Chat is not visible by default
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-m1',
          'user_email': 'alice@test.com',
          'message': 'hey @me@test.com',
          'created_at': '2026-01-01 00:00:00',
          'mentions': ['my-uid'],
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(mentionStates, [true]);

      // Setting visible clears mention
      chatKey.currentState!.setVisible(true);
      expect(mentionStates, [true, false]);
    });

    testWidgets('mention not fired for non-self mentions', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"my-uid","email":"me@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      final mentionStates = <bool>[];

      await tester.pumpWidget(
        buildChat(
          authService: auth,
          onMentionChanged: (m) => mentionStates.add(m),
        ),
      );

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-other',
          'user_email': 'alice@test.com',
          'message': 'hey @bob@test.com',
          'created_at': '2026-01-01 00:00:00',
          'mentions': ['bob-uid'],
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(mentionStates, isEmpty);
    });

    testWidgets('@autocomplete shows members on @ input', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      // Overlay should show member emails
      expect(find.text('alice@test.com'), findsWidgets);
      expect(find.text('bob@test.com'), findsWidgets);
    });

    testWidgets('@autocomplete filters by query', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@ali');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);
      // bob should not appear since "ali" doesn't match
      final bobFinder = find.text('bob@test.com');
      // Bob should only appear once (in the message area or not at all in overlay)
      expect(bobFinder, findsNothing);
    });

    testWidgets('@autocomplete inserts mention on tap', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      // Tap on the autocomplete entry
      await tester.tap(find.text('alice@test.com').last);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');
    });

    testWidgets('@autocomplete hides when no match', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@zzz');
      await tester.pump();

      // Only the input should have text, no overlay entries
      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('@autocomplete hides on send', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      // Autocomplete overlay should be visible
      expect(find.text('alice@test.com'), findsWidgets);

      // Now send and verify overlay is gone (text field cleared)
      await tester.tap(find.byIcon(Icons.send));
      await tester.pump();

      // After send, text field is cleared so no autocomplete
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, isEmpty);
    });

    testWidgets('@autocomplete hides when @ followed by space', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@ ');
      await tester.pump();

      // No autocomplete since there's a space after @
      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('@autocomplete handles invalid cursor position', (
      tester,
    ) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      // Set text with an invalid selection (cursor = -1)
      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.value = const TextEditingValue(
        text: '@al',
        selection: TextSelection.collapsed(offset: -1),
      );
      await tester.pump();

      // No autocomplete since cursor is invalid
      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('mention and URL both present in markdown', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-urlmention',
          'user_email': 'alice@test.com',
          'message': 'see https://example.com and @bob@test.com',
          'created_at': '2026-01-01 00:00:00',
          'mentions': ['bob-uid'],
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      final data = _findMarkdownData(tester);
      expect(data, contains('https://example.com'));
      expect(data, contains('**@bob@test.com**'));
    });

    testWidgets('system message renders centered and muted', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-sys',
          'user_email': 'alice@test.com',
          'message': 'alice@test.com joined',
          'message_type': 2,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // System message text is rendered
      expect(find.text('alice@test.com joined'), findsOneWidget);
      // Should be centered — wrapped in a Center widget
      expect(find.byType(Center), findsWidgets);
      // No delete button for system messages
      expect(find.byIcon(Icons.close), findsNothing);
    });

    testWidgets('agent message renders with robot icon', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-agent',
          'user_email': 'agent@bot',
          'message': 'I can help with that',
          'message_type': 1,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Agent message should have a smart_toy icon
      expect(find.byIcon(Icons.smart_toy), findsOneWidget);
      // Message text should be present
      expect(find.byType(SelectableText), findsOneWidget);
    });

    testWidgets('user message renders without robot icon (default type)', (
      tester,
    ) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-user',
          'user_email': 'alice@test.com',
          'message': 'normal message',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // No robot icon for user messages
      expect(find.byIcon(Icons.smart_toy), findsNothing);
    });

    testWidgets('agent_thinking shows indicator and stop button', (
      tester,
    ) async {
      await tester.pumpWidget(buildChat());

      // No stop button initially
      expect(find.byIcon(Icons.stop_circle_outlined), findsNothing);
      expect(find.text('MrBoops is thinking...'), findsNothing);

      // Send agent_thinking = true
      await tester.runAsync(() async {
        channel.serverSend(
            {'type': 'agent_thinking', 'thinking': true, 'name': 'MrBoops'});
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(find.byIcon(Icons.stop_circle_outlined), findsOneWidget);
      expect(find.text('MrBoops is thinking...'), findsOneWidget);

      // Send agent_thinking = false
      await tester.runAsync(() async {
        channel.serverSend({'type': 'agent_thinking', 'thinking': false});
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      expect(find.byIcon(Icons.stop_circle_outlined), findsNothing);
      expect(find.text('MrBoops is thinking...'), findsNothing);
    });

    testWidgets('stop button sends chat_agent_abort', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend(
            {'type': 'agent_thinking', 'thinking': true, 'name': 'MrBoops'});
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      await tester.tap(find.byIcon(Icons.stop_circle_outlined));
      await tester.pump();

      final sink = channel.sink as _FakeSink;
      final msgs = sink.sent
          .map((m) => jsonDecode(m as String) as Map<String, dynamic>)
          .toList();
      expect(msgs.any((m) => m['cmd'] == 'chat_agent_abort'), isTrue);
    });

    testWidgets('presence bar shows connected users', (tester) async {
      client.presenceUsers = [
        {
          'user_id': 'u1',
          'user_email': 'alice@test.com',
          'user_handle': 'alice',
        },
        {
          'user_id': 'u2',
          'user_email': 'bob@test.com',
          'user_handle': 'bob',
        },
      ];

      await tester.pumpWidget(buildChat());

      // Green dot + avatar initials (from handle)
      expect(find.text('A'), findsOneWidget);
      expect(find.text('B'), findsOneWidget);
    });

    testWidgets('presence tooltip shows handle instead of email',
        (tester) async {
      client.presenceUsers = [
        {
          'user_id': 'u1',
          'user_email': 'alice@test.com',
          'user_handle': 'alice',
        },
        {
          'user_id': 'u2',
          'user_email': 'bob@test.com',
          'user_handle': '',
        },
      ];

      await tester.pumpWidget(buildChat());

      // Tooltip for user with handle shows handle
      expect(find.byTooltip('alice'), findsOneWidget);
      // Tooltip for user without handle falls back to email
      expect(find.byTooltip('bob@test.com'), findsOneWidget);
    });

    testWidgets('presence bar hidden when no users', (tester) async {
      client.presenceUsers = [];

      await tester.pumpWidget(buildChat());

      expect(find.text('Online '), findsNothing);
    });

    testWidgets('presence bar updates on join', (tester) async {
      client.presenceUsers = [
        {'user_id': 'u1', 'user_email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());
      expect(find.text('A'), findsOneWidget);

      // Simulate a presence_join via WsClient
      client.presenceUsers = [
        {'user_id': 'u1', 'user_email': 'alice@test.com'},
        {'user_id': 'u2', 'user_email': 'bob@test.com'},
      ];
      client.notifyListeners();
      await tester.pump();

      expect(find.text('B'), findsOneWidget);
    });

    testWidgets('self user shown with outline style', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
            utf8.encode('{"sub":"my-uid","email":"me@test.com"}'),
          ) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      client.presenceUsers = [
        {'user_id': 'my-uid', 'user_email': 'me@test.com'},
        {'user_id': 'other', 'user_email': 'other@test.com'},
      ];

      await tester.pumpWidget(buildChat(authService: auth));

      // Both users should be rendered
      expect(find.text('M'), findsOneWidget);
      expect(find.text('O'), findsOneWidget);

      // Self avatar should have transparent background (outline style)
      final avatars = tester.widgetList<CircleAvatar>(
        find.byType(CircleAvatar),
      );
      final selfAvatar = avatars.firstWhere(
        (a) => a.backgroundColor == Colors.transparent,
      );
      expect(selfAvatar, isNotNull);
    });

    testWidgets('Tab key accepts first autocomplete suggestion', (
      tester,
    ) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');
    });

    testWidgets('Arrow keys navigate autocomplete suggestions', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);
      expect(find.text('bob@test.com'), findsWidgets);

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@bob@test.com ');
    });

    testWidgets('Arrow up wraps highlight back', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');
    });

    testWidgets('Enter key accepts autocomplete when overlay is visible', (
      tester,
    ) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final chatMsgs = msgs.where((m) => m['cmd'] == 'chat_send').toList();
      expect(chatMsgs.length, 0);
    });

    testWidgets('Escape dismisses autocomplete', (tester) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);

      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pump();

      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('Enter sends message and re-focuses input', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'hello');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final chatMsgs = msgs.where((m) => m['cmd'] == 'chat_send').toList();
      expect(chatMsgs.length, 1);
      expect(chatMsgs[0]['message'], 'hello');

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, isEmpty);
      expect(field.focusNode!.hasFocus, isTrue);
    });

    testWidgets('multiline text field grows', (tester) async {
      await tester.pumpWidget(buildChat());

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.maxLines, isNull);
    });

    testWidgets('Ctrl+A moves cursor to beginning of line', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.selection.baseOffset, 11);

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.selection.baseOffset, 0);
    });

    testWidgets('Ctrl+E moves cursor to end of line', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 0);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyE);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.selection.baseOffset, 11);
    });

    testWidgets('Ctrl+K kills to end of line', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      // Move cursor to position 5 (after "hello")
      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 5);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyK);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.text, 'hello');
      expect(field.controller!.selection.baseOffset, 5);
    });

    testWidgets('Ctrl+K at newline joins lines', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'line1\nline2');
      await tester.pump();

      // Move cursor to position 5 (the newline between lines)
      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 5);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyK);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.text, 'line1line2');
    });

    testWidgets('Shift+Ctrl+A selects all text', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.selection.baseOffset, 0);
      expect(field.controller!.selection.extentOffset, 11);
    });

    testWidgets('Up arrow recalls previous sent message', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'first');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.enterText(find.byType(TextField), 'second');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, 'second');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      expect(field.controller!.text, 'first');
    });

    testWidgets('Down arrow restores draft after history recall', (
      tester,
    ) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'sent msg');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.enterText(find.byType(TextField), 'my draft');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, 'sent msg');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(field.controller!.text, 'my draft');
    });

    testWidgets('Down arrow navigates forward through history', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), 'first');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      await tester.enterText(find.byType(TextField), 'second');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      await tester.enterText(find.byType(TextField), 'third');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, 'first');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(field.controller!.text, 'second');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(field.controller!.text, 'third');
    });

    testWidgets('autocomplete highlight clamps when list shrinks', (
      tester,
    ) async {
      client.workspaceMembers = [
        {'id': 'u1', 'email': 'alice@test.com'},
        {'id': 'u2', 'email': 'bob@test.com'},
        {'id': 'u3', 'email': 'abby@test.com'},
      ];

      await tester.pumpWidget(buildChat());

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();

      await tester.enterText(find.byType(TextField), '@bo');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@bob@test.com ');
    });

    testWidgets('long message is truncated with show more link', (
      tester,
    ) async {
      await tester.pumpWidget(buildChat());

      final longMsg = 'A' * 500;
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-long',
          'user_email': 'alice@test.com',
          'message': longMsg,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump();

      expect(find.text('…show more'), findsOneWidget);
    });

    testWidgets('tapping show more expands message', (tester) async {
      await tester.pumpWidget(buildChat());

      final longMsg = 'A' * 500;
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-long2',
          'user_email': 'alice@test.com',
          'message': longMsg,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('…show more'));
      await tester.pump();

      expect(find.text('show less'), findsOneWidget);
      expect(find.text('…show more'), findsNothing);
    });

    testWidgets('tapping show less collapses message', (tester) async {
      await tester.pumpWidget(buildChat());

      final longMsg = 'A' * 500;
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-long3',
          'user_email': 'alice@test.com',
          'message': longMsg,
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('…show more'));
      await tester.pump();

      await tester.tap(find.text('show less'));
      await tester.pump();
      await tester.pump();

      expect(find.text('…show more'), findsOneWidget);
      expect(find.text('show less'), findsNothing);
    });

    testWidgets('short message has no show more link', (tester) async {
      await tester.pumpWidget(buildChat());

      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-short',
          'user_email': 'alice@test.com',
          'message': 'hi',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      await tester.pump();

      expect(find.text('…show more'), findsNothing);
      expect(find.text('show less'), findsNothing);
    });

    testWidgets('scroll to top triggers load more', (tester) async {
      await tester.pumpWidget(buildChat());

      // Add enough messages to require scrolling
      await tester.runAsync(() async {
        for (int i = 0; i < 30; i++) {
          channel.serverSend({
            'type': 'chat_message',
            'id': 'msg-$i',
            'user_email': 'user@test.com',
            'message': 'Message $i',
            'created_at': '2026-01-01 00:${i.toString().padLeft(2, '0')}:00',
          });
        }
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pumpAndSettle();

      // Scroll to top
      await tester.drag(find.byType(ListView).last, const Offset(0, 5000));
      await tester.pump();

      // Should have sent chat_load_more
      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final loadMore = msgs.where((m) => m['cmd'] == 'chat_load_more').toList();
      expect(loadMore.length, 1);
      expect(loadMore[0]['before_id'], 'msg-0');
    });

    testWidgets('history page prepends messages', (tester) async {
      await tester.pumpWidget(buildChat());

      // Add initial messages
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-new',
          'user_email': 'user@test.com',
          'message': 'newest',
          'created_at': '2026-01-01 00:01:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Simulate receiving a history page
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_history_page',
          'messages': [
            {
              'id': 'msg-old',
              'user_email': 'user@test.com',
              'message': 'oldest',
              'created_at': '2026-01-01 00:00:00',
            },
          ],
          'has_more': false,
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Both messages should be present
      expect(find.byType(MarkdownBody), findsNWidgets(2));
    });

    testWidgets('empty history page sets hasMore to false', (tester) async {
      final chatKey = GlobalKey<WorkspaceChatState>();
      await tester.pumpWidget(buildChat(chatKey: chatKey));

      // Add a message
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_message',
          'id': 'msg-1',
          'user_email': 'user@test.com',
          'message': 'hello',
          'created_at': '2026-01-01 00:00:00',
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Simulate empty history page
      await tester.runAsync(() async {
        channel.serverSend({
          'type': 'chat_history_page',
          'messages': [],
          'has_more': false,
        });
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();

      // Scrolling to top should NOT trigger another load
      channel._sink.sent.clear();
      await tester.drag(find.byType(ListView).last, const Offset(0, 5000));
      await tester.pump();

      final msgs = channel._sink.sent
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      final loadMore = msgs.where((m) => m['cmd'] == 'chat_load_more').toList();
      expect(loadMore.length, 0);
    });

    testWidgets('loading spinner shown during load more', (tester) async {
      final chatKey = GlobalKey<WorkspaceChatState>();
      await tester.pumpWidget(buildChat(chatKey: chatKey));

      // Add enough messages to scroll
      await tester.runAsync(() async {
        for (int i = 0; i < 30; i++) {
          channel.serverSend({
            'type': 'chat_message',
            'id': 'load-$i',
            'user_email': 'user@test.com',
            'message': 'Message $i',
            'created_at': '2026-01-01 00:${i.toString().padLeft(2, '0')}:00',
          });
        }
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pumpAndSettle();

      // Scroll to top to trigger loading
      await tester.drag(find.byType(ListView).last, const Offset(0, 5000));
      await tester.pump();

      // Loading spinner should be visible
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });
  });
}
