import 'dart:async';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../ws/ws_client.dart';
import '../theme/colors.dart';
import '../auth/auth_service.dart';
import 'agent_thinking_indicator.dart';
import 'chat_input_bar.dart';
import 'chat_message_list.dart';
import 'chat_presence_bar.dart';

/// Per-workspace real-time chat panel.
class WorkspaceChat extends StatefulWidget {
  final WsClient wsClient;

  /// Called when the unread message count changes.
  final ValueChanged<int>? onUnreadChanged;

  /// Called when mention-while-hidden state changes.
  final ValueChanged<bool>? onMentionChanged;

  const WorkspaceChat({
    super.key,
    required this.wsClient,
    this.onUnreadChanged,
    this.onMentionChanged,
  });

  @override
  State<WorkspaceChat> createState() => WorkspaceChatState();
}

class WorkspaceChatState extends State<WorkspaceChat> {
  final List<Map<String, dynamic>> _messages = [];
  final _scrollController = ScrollController();
  final _inputBarKey = GlobalKey<ChatInputBarState>();
  StreamSubscription<Map<String, dynamic>>? _chatSub;
  StreamSubscription<Map<String, dynamic>>? _historyPageSub;
  int _unreadCount = 0;
  bool _isVisible = false;
  bool _hasMention = false;
  bool _loadingOlder = false;

  bool _agentThinking = false;
  String _agentName = 'agent';
  bool _hasMore = true;

  // Expanded message IDs (for long message truncation)
  final Set<String> _expandedMessages = {};

  @override
  void initState() {
    super.initState();
    if (widget.wsClient.chatHistory.isNotEmpty) {
      _messages.addAll(widget.wsClient.chatHistory);
    }
    _chatSub = widget.wsClient.chatMessages.listen(_onMessage);
    _historyPageSub = widget.wsClient.chatHistoryPages.listen(_onHistoryPage);
    _scrollController.addListener(_onScroll);
    widget.wsClient.addListener(_onPresenceChanged);
    if (_messages.isNotEmpty) {
      _scrollToBottom();
    }
  }

  /// Focuses the message input. Called by the parent when the Chat tab is
  /// selected so the user can type immediately without an extra click.
  void requestFocus() => _inputBarKey.currentState?.requestFocus();

  /// Called by the parent when this tab becomes visible/hidden.
  void setVisible(bool visible) {
    _isVisible = visible;
    if (visible && _unreadCount > 0) {
      _unreadCount = 0;
      _hasMention = false;
      widget.onUnreadChanged?.call(0);
      widget.onMentionChanged?.call(false);
    }
  }

  void _onMessage(Map<String, dynamic> msg) {
    if (!mounted) return;
    final type = msg['type'] as String?;

    if (type == 'agent_thinking') {
      setState(() {
        _agentThinking = msg['thinking'] as bool? ?? false;
        if (_agentThinking) {
          _agentName = msg['name'] as String? ?? 'agent';
        }
      });
      return;
    }

    if (type == 'chat_updated') {
      final updatedId = msg['message_id'] as String?;
      final newText = msg['message'] as String?;
      if (updatedId != null && newText != null) {
        setState(() {
          final idx = _messages.indexWhere((m) => m['id'] == updatedId);
          if (idx >= 0) {
            _messages[idx] = {..._messages[idx], 'message': newText};
          }
        });
      }
      return;
    }

    // Regular chat_message or chat_history item
    setState(() => _messages.add(msg));

    // Don't count system messages (join/leave) as unread.
    final isSystem = (msg['message_type'] as int? ?? 0) == 2;
    if (!_isVisible && !isSystem) {
      _unreadCount++;
      widget.onUnreadChanged?.call(_unreadCount);

      final mentions = msg['mentions'] as List?;
      if (mentions != null && !_hasMention) {
        final currentUserId = context.read<AuthService>().userId;
        if (mentions.contains(currentUserId)) {
          _hasMention = true;
          widget.onMentionChanged?.call(true);
        }
      }
    }

    _scrollToBottom();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scrollController.hasClients) {
          _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
        }
      });
    });
  }

  void _onScroll() {
    if (!_hasMore || _loadingOlder) return;
    if (!_scrollController.hasClients) return;
    if (_scrollController.position.pixels <= 0 && _messages.isNotEmpty) {
      _loadOlderMessages();
    }
  }

  void _loadOlderMessages() {
    final oldestId = _messages.first['id'] as String?;
    if (oldestId == null) return;
    setState(() => _loadingOlder = true);
    widget.wsClient.sendChatLoadMore(oldestId);
  }

  void _onHistoryPage(Map<String, dynamic> page) {
    if (!mounted) return;
    final messages = page['messages'] as List? ?? [];
    final hasMore = page['has_more'] as bool? ?? false;

    if (messages.isEmpty) {
      setState(() {
        _loadingOlder = false;
        _hasMore = false;
      });
      return;
    }

    final scrollBefore =
        _scrollController.hasClients ? _scrollController.position.pixels : 0.0;
    final maxBefore = _scrollController.hasClients
        ? _scrollController.position.maxScrollExtent
        : 0.0;

    setState(() {
      final older = messages.cast<Map<String, dynamic>>();
      _messages.insertAll(0, older);
      _loadingOlder = false;
      _hasMore = hasMore;
    });

    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        final maxAfter = _scrollController.position.maxScrollExtent;
        final delta = maxAfter - maxBefore;
        _scrollController.jumpTo(scrollBefore + delta);
      }
    });
  }

  void _onPresenceChanged() {
    if (mounted) setState(() {});
  }

  void _onSendText(String text) {
    widget.wsClient.sendChatMessage(text);
  }

  void _onDeleteMessage(String messageId) {
    widget.wsClient.sendChatDelete(messageId);
  }

  void _onToggleExpand(String messageId) {
    setState(() {
      if (_expandedMessages.contains(messageId)) {
        _expandedMessages.remove(messageId);
      } else {
        _expandedMessages.add(messageId);
      }
    });
  }

  @override
  void dispose() {
    widget.wsClient.removeListener(_onPresenceChanged);
    _chatSub?.cancel();
    _historyPageSub?.cancel();
    _scrollController.removeListener(_onScroll);
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.read<AuthService>();
    final currentUserId = auth.userId;

    return Container(
      color: KColors.bgCanvas,
      child: Column(
        children: [
          ChatPresenceBar(
            users: widget.wsClient.presenceUsers,
            currentUserId: currentUserId,
          ),
          ChatMessageList(
            messages: _messages,
            scrollController: _scrollController,
            currentUserId: currentUserId,
            loadingOlder: _loadingOlder,
            expandedMessages: _expandedMessages,
            onToggleExpand: _onToggleExpand,
            onDelete: _onDeleteMessage,
          ),
          if (_agentThinking) AgentThinkingIndicator(agentName: _agentName),
          ChatInputBar(
            key: _inputBarKey,
            onSend: () {},
            onSendText: _onSendText,
            agentThinking: _agentThinking,
            onAbort: () => widget.wsClient.sendChatAgentAbort(),
            members: widget.wsClient.workspaceMembers,
          ),
        ],
      ),
    );
  }
}
