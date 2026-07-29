import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_feature_chat/feature.dart';
import 'package:klangk_feature_chat/workspace_chat.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';

/// A minimal ChatServices backed by in-memory streams + a capture list, so
/// the feature's widgets can be pumped without the host's real WS client.
class _FakeChat extends ChangeNotifier implements ChatServices {
  @override
  final List<Map<String, dynamic>> chatHistory = [];
  final List<String> sent = [];
  final _messages = StreamController<Map<String, dynamic>>.broadcast();

  @override
  List<Map<String, dynamic>> presenceUsers = [];

  @override
  Stream<Map<String, dynamic>> get chatMessages => _messages.stream;

  @override
  Stream<Map<String, dynamic>> chatHistoryPages = const Stream.empty();

  @override
  List<Map<String, dynamic>> get mentionCandidates => presenceUsers;

  @override
  void sendChatMessage(String text) => sent.add(text);

  @override
  void sendChatLoadMore(String beforeId, {int limit = 50}) {}

  @override
  void sendChatDelete(String messageId) {}

  @override
  void sendChatAgentAbort() {}
}

class _FakeServices implements WorkspaceServices {
  _FakeServices(this.chat, this.currentUserId);
  @override
  final ChatServices? chat;
  @override
  final String? currentUserId;
}

void main() {
  group('ChatTab', () {
    test('exposes title, icon, and a null badge by default', () {
      final tab = ChatTab();
      addTearDown(tab.dispose);
      expect(tab.title, 'Chat');
      expect(tab.icon, Icons.chat_outlined);
      expect(tab.badge!.value, isNull);
    });

    test('dispose() leaves the badge usable (singleton reused per-workspace)',
        () {
      // WorkspacePage.dispose() calls tab.dispose() on every workspace close,
      // but ChatTab is a singleton reused across pages — disposing _badge
      // would break the next workspace's IdeLayout badge subscription
      // (use-after-dispose: addListener on a disposed notifier throws).
      final tab = ChatTab();
      tab.dispose();
      expect(tab.badge, isNotNull);
      expect(() => tab.badge!.addListener(() {}), returnsNormally);
    });

    testWidgets('renders WorkspaceChat when chat is available', (tester) async {
      final chat = _FakeChat();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Provider<WorkspaceServices>.value(
              value: _FakeServices(chat, 'me'),
              child: Builder(builder: ChatTab().build),
            ),
          ),
        ),
      );
      expect(find.byType(WorkspaceChat), findsOneWidget);
    });
  });
}
