import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_feature_chat/workspace_chat.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A minimal ChatServices backed by in-memory streams + capture lists, so the
/// WorkspaceChat orchestration can be driven without the host's real WS client.
///
/// `serverSend` feeds [chatMessages] (a live incoming message); the chat's
/// `sendX` methods record what the chat tried to send. Extends ChangeNotifier
/// so the chat's `addListener`/`removeListener` (presence rebuild) works.
class _FakeChat extends ChangeNotifier implements ChatServices {
  _FakeChat({List<Map<String, dynamic>>? history})
      : chatHistory = history != null ? List.of(history) : [];

  @override
  final List<Map<String, dynamic>> chatHistory;
  final List<String> sent = [];
  final List<String> deleted = [];
  final List<String> loadedMore = [];
  bool aborted = false;

  @override
  List<Map<String, dynamic>> presenceUsers = [];

  final _messages = StreamController<Map<String, dynamic>>.broadcast();
  final _historyPages = StreamController<Map<String, dynamic>>.broadcast();

  @override
  Stream<Map<String, dynamic>> get chatMessages => _messages.stream;
  @override
  Stream<Map<String, dynamic>> get chatHistoryPages => _historyPages.stream;
  @override
  List<Map<String, dynamic>> get mentionCandidates => presenceUsers;

  /// Feed a live incoming chat/agent message into [chatMessages].
  void serverSend(Map<String, dynamic> msg) => _messages.add(msg);

  /// Feed an older-history page into [chatHistoryPages] (load-more response).
  void serverSendHistoryPage(Map<String, dynamic> page) =>
      _historyPages.add(page);

  @override
  void sendChatMessage(String text) => sent.add(text);
  @override
  void sendChatLoadMore(String beforeId, {int limit = 50}) =>
      loadedMore.add(beforeId);
  @override
  void sendChatDelete(String messageId) => deleted.add(messageId);
  @override
  void sendChatAgentAbort() => aborted = true;

  @override
  void dispose() {
    _messages.close();
    _historyPages.close();
    super.dispose();
  }
}

Widget _harness(
  _FakeChat chat, {
  String? currentUserId = 'me',
  ValueChanged<int>? onUnreadChanged,
  ValueChanged<bool>? onMentionChanged,
  GlobalKey<WorkspaceChatState>? chatKey,
}) {
  return MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: 800,
        height: 600,
        child: WorkspaceChat(
          key: chatKey,
          chat: chat,
          currentUserId: currentUserId,
          onUnreadChanged: onUnreadChanged,
          onMentionChanged: onMentionChanged,
        ),
      ),
    ),
  );
}

Map<String, dynamic> _msg(String id, String text, {String? userId = 'other'}) =>
    {'id': id, 'message': text, 'user_id': userId, 'message_type': 0};

void main() {
  group('WorkspaceChat orchestration', () {
    testWidgets('renders an incoming message', (tester) async {
      final chat = _FakeChat();
      await tester.pumpWidget(_harness(chat));
      chat.serverSend(_msg('1', 'hello'));
      await tester.pumpAndSettle();
      expect(find.text('hello'), findsOneWidget);
    });

    testWidgets('sends a message on Enter', (tester) async {
      final chat = _FakeChat();
      await tester.pumpWidget(_harness(chat));
      await tester.enterText(find.byType(TextField), 'hi there');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();
      expect(chat.sent, contains('hi there'));
    });

    testWidgets('unread count increments while hidden, clears on setVisible',
        (tester) async {
      final chat = _FakeChat();
      final unread = <int>[];
      final key = GlobalKey<WorkspaceChatState>();
      await tester.pumpWidget(_harness(
        chat,
        onUnreadChanged: unread.add,
        chatKey: key,
      ));
      // Two messages arrive while the tab is hidden (not visible).
      chat.serverSend(_msg('1', 'one'));
      chat.serverSend(_msg('2', 'two'));
      await tester.pumpAndSettle();
      expect(unread, contains(2));
      // Becoming visible clears the unread count.
      key.currentState!.setVisible(true);
      await tester.pumpAndSettle();
      expect(unread.last, 0);
    });

    testWidgets('onMentionChanged fires when @-mentioned while hidden',
        (tester) async {
      final chat = _FakeChat();
      final mentions = <bool>[];
      final key = GlobalKey<WorkspaceChatState>();
      await tester.pumpWidget(_harness(
        chat,
        currentUserId: 'me',
        onMentionChanged: mentions.add,
        chatKey: key,
      ));
      chat.serverSend(_msg('1', 'hey', userId: 'other')..['mentions'] = ['me']);
      await tester.pumpAndSettle();
      expect(mentions, contains(true));
      // setVisible(true) clears the mention flag.
      key.currentState!.setVisible(true);
      await tester.pumpAndSettle();
      expect(mentions.last, isFalse);
    });

    testWidgets('mention does not fire for non-self mentions', (tester) async {
      final chat = _FakeChat();
      final mentions = <bool>[];
      await tester.pumpWidget(
        _harness(chat, currentUserId: 'me', onMentionChanged: mentions.add),
      );
      chat.serverSend(_msg('1', 'hey')..['mentions'] = ['someone-else']);
      await tester.pumpAndSettle();
      expect(mentions, isEmpty);
    });

    testWidgets('chat_history_replace clears and replaces the list',
        (tester) async {
      final chat = _FakeChat(history: [_msg('old', 'stale')]);
      await tester.pumpWidget(_harness(chat));
      await tester.pumpAndSettle();
      expect(find.text('stale'), findsOneWidget);
      chat.serverSend({
        'type': 'chat_history_replace',
        'messages': [_msg('new', 'fresh')],
      });
      await tester.pumpAndSettle();
      expect(find.text('stale'), findsNothing);
      expect(find.text('fresh'), findsOneWidget);
    });

    testWidgets('loads buffered chat history on init', (tester) async {
      final chat = _FakeChat(history: [_msg('1', 'buffered')]);
      await tester.pumpWidget(_harness(chat));
      await tester.pumpAndSettle();
      expect(find.text('buffered'), findsOneWidget);
    });

    testWidgets('agent_thinking toggles the indicator + abort sends',
        (tester) async {
      final chat = _FakeChat();
      await tester.pumpWidget(_harness(chat));
      expect(find.byIcon(Icons.stop_circle_outlined), findsNothing);
      await tester.runAsync(() async {
        chat.serverSend(
            {'type': 'agent_thinking', 'thinking': true, 'name': 'klangk'});
        await Future.delayed(Duration.zero);
        await Future.delayed(Duration.zero);
      });
      await tester.pump();
      expect(find.text('klangk is thinking...'), findsOneWidget);
      final abort = find.byIcon(Icons.stop_circle_outlined);
      expect(abort, findsOneWidget);
      await tester.tap(abort);
      await tester.pump();
      expect(chat.aborted, isTrue);
    });
  });
}
