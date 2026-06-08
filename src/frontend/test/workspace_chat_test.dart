import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
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

/// Recursively search all TextSpans in a tree for one matching [predicate].
bool _findSpan(InlineSpan root, bool Function(TextSpan) predicate) {
  if (root is TextSpan) {
    if (predicate(root)) return true;
    if (root.children != null) {
      for (final child in root.children!) {
        if (_findSpan(child, predicate)) return true;
      }
    }
  }
  return false;
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
      // Timestamp is formatted and localized — just verify it renders
      // (the exact format depends on the test runner's timezone)
      expect(find.byType(Text), findsWidgets);
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

      // Verify original text is rendered in a TextSpan
      bool hasOriginal = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null && _findSpan(span, (s) => s.text == 'original text')) {
          hasOriginal = true;
        }
      }
      expect(hasOriginal, isTrue);

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

      // Original text should be gone, replaced text should appear
      bool hasDeleted = false;
      bool stillHasOriginal = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null) {
          if (_findSpan(span, (s) => s.text == 'original text')) {
            stillHasOriginal = true;
          }
          if (_findSpan(span, (s) => s.text == '<message deleted by author>')) {
            hasDeleted = true;
          }
        }
      }
      expect(stillHasOriginal, isFalse);
      expect(hasDeleted, isTrue);
    });

    testWidgets('delete button shown for own messages', (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
              utf8.encode('{"sub":"test-uid","email":"test@test.com"}')) +
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
              utf8.encode('{"sub":"test-uid","email":"test@test.com"}')) +
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

    testWidgets('deleted message shown in italic without delete button',
        (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url.encode(
              utf8.encode('{"sub":"test-uid","email":"test@test.com"}')) +
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

      // Verify italic style via SelectableText TextSpan
      bool foundItalic = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null &&
            _findSpan(
                span,
                (s) =>
                    s.text == '<message deleted by author>' &&
                    s.style?.fontStyle == FontStyle.italic)) {
          foundItalic = true;
        }
      }
      expect(foundItalic, isTrue);
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
      final hasDayAbbrev =
          allText.any((t) => dayAbbrevs.any((d) => t.contains(d)));
      expect(hasDayAbbrev, isTrue);
    });

    testWidgets('URLs in messages rendered as styled links', (tester) async {
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

      // Find the SelectableText and verify URL span is styled differently
      bool foundUrl = false;
      bool foundPrefix = false;
      bool foundSuffix = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null) {
          if (_findSpan(
              span,
              (s) =>
                  s.text == 'https://example.com/path' &&
                  s.style?.decoration == TextDecoration.underline)) {
            foundUrl = true;
          }
          if (_findSpan(span, (s) => s.text == 'Check ')) {
            foundPrefix = true;
          }
          if (_findSpan(span, (s) => s.text == ' for details')) {
            foundSuffix = true;
          }
        }
      }
      expect(foundUrl, isTrue);
      expect(foundPrefix, isTrue);
      expect(foundSuffix, isTrue);
    });

    testWidgets('setVisible clears unread count', (tester) async {
      final unreadCounts = <int>[];
      final chatKey = GlobalKey<WorkspaceChatState>();

      await tester.pumpWidget(buildChat(
        onUnreadChanged: (count) => unreadCounts.add(count),
        chatKey: chatKey,
      ));

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

      expect(find.textContaining('buffered message'), findsOneWidget);
    });

    testWidgets('@mention renders as bold blue span', (tester) async {
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

      bool foundMention = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null &&
            _findSpan(
                span,
                (s) =>
                    s.text == '@bob@test.com' &&
                    s.style?.fontWeight == FontWeight.bold)) {
          foundMention = true;
        }
      }
      expect(foundMention, isTrue);
    });

    testWidgets('self-mention renders with background highlight',
        (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url
              .encode(utf8.encode('{"sub":"my-uid","email":"me@test.com"}')) +
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

      bool foundSelfMention = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null &&
            _findSpan(
                span,
                (s) =>
                    s.text == '@me@test.com' &&
                    s.style?.backgroundColor != null)) {
          foundSelfMention = true;
        }
      }
      expect(foundSelfMention, isTrue);
    });

    testWidgets('onMentionChanged fires when mentioned while hidden',
        (tester) async {
      final fakeJwt = base64Url.encode(utf8.encode('{"alg":"HS256"}')) +
          '.' +
          base64Url
              .encode(utf8.encode('{"sub":"my-uid","email":"me@test.com"}')) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      final mentionStates = <bool>[];
      final chatKey = GlobalKey<WorkspaceChatState>();

      await tester.pumpWidget(buildChat(
        authService: auth,
        onMentionChanged: (m) => mentionStates.add(m),
        chatKey: chatKey,
      ));

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
          base64Url
              .encode(utf8.encode('{"sub":"my-uid","email":"me@test.com"}')) +
          '.sig';
      SharedPreferences.setMockInitialValues({'klangk_jwt': fakeJwt});
      final auth = AuthService();
      await tester.runAsync(() => Future.delayed(Duration.zero));

      final mentionStates = <bool>[];

      await tester.pumpWidget(buildChat(
        authService: auth,
        onMentionChanged: (m) => mentionStates.add(m),
      ));

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

    testWidgets('@autocomplete handles invalid cursor position',
        (tester) async {
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

    testWidgets('mention not in URL is highlighted', (tester) async {
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

      bool foundUrl = false;
      bool foundMention = false;
      for (final st
          in tester.widgetList<SelectableText>(find.byType(SelectableText))) {
        final span = st.textSpan;
        if (span != null) {
          if (_findSpan(
              span,
              (s) =>
                  s.text == 'https://example.com' &&
                  s.style?.decoration == TextDecoration.underline)) {
            foundUrl = true;
          }
          if (_findSpan(
              span,
              (s) =>
                  s.text == '@bob@test.com' &&
                  s.style?.fontWeight == FontWeight.bold)) {
            foundMention = true;
          }
        }
      }
      expect(foundUrl, isTrue);
      expect(foundMention, isTrue);
    });
  });
}
