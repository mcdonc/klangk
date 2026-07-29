import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'workspace_chat.dart';

/// The chat workspace tab (#1976).
///
/// Renders [WorkspaceChat] from the host's [WorkspaceServices], and exposes
/// the chat's unread/mention state as the tab's strip badge
/// ([WorkspaceTabPlugin.badge]). Mark-read-on-view and focus-on-select flow
/// through [setVisible] to the underlying [WorkspaceChatState].
class ChatTab extends WorkspaceTabPlugin {
  final GlobalKey<WorkspaceChatState> _chatKey = GlobalKey();
  final ValueNotifier<TabBadge?> _badge = ValueNotifier<TabBadge?>(null);
  int _unread = 0;
  bool _mention = false;

  @override
  String get title => 'Chat';

  @override
  IconData get icon => Icons.chat_outlined;

  @override
  ValueListenable<TabBadge?>? get badge => _badge;

  @override
  Widget build(BuildContext context) {
    final services = context.read<WorkspaceServices>();
    final chat = services.chat;
    if (chat == null) {
      return const Center(
        child: Text('Chat is unavailable in this workspace.'),
      );
    }
    return WorkspaceChat(
      key: _chatKey,
      chat: chat,
      currentUserId: services.currentUserId,
      onUnreadChanged: _onUnread,
      onMentionChanged: _onMention,
    );
  }

  void _onUnread(int count) {
    _unread = count;
    _syncBadge();
  }

  void _onMention(bool mentioned) {
    _mention = mentioned;
    _syncBadge();
  }

  void _syncBadge() {
    _badge.value = (_unread > 0 || _mention)
        ? TabBadge(count: _unread, highlight: _mention)
        : null;
  }

  @override
  void setVisible(bool visible) {
    final state = _chatKey.currentState;
    if (state == null) return;
    state.setVisible(visible);
    if (visible) state.requestFocus();
  }

  /// No-op by design. [ChatTab] is a singleton — registered once into
  /// [WorkspaceTabRegistry] in main() and reused across workspace pages — so
  /// [_badge] is app-lifetime. `WorkspacePage.dispose()` calls `dispose()` on
  /// every workspace close; disposing [_badge] here would break the *next*
  /// workspace's IdeLayout badge subscription (a ValueNotifier used after
  /// dispose). IdeLayout owns and cleans up its own per-page badge listeners
  /// (`_disposeFeatureBadgeListeners`), so there is nothing to release here.
  @override
  void dispose() {}
}
