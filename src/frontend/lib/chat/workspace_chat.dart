import 'dart:async';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import '../ws/ws_client.dart';
import '../theme/colors.dart';
import '../auth/auth_service.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.html) '../utils/web_helpers_web.dart';
import 'package:provider/provider.dart';

/// Per-workspace real-time chat panel.
class WorkspaceChat extends StatefulWidget {
  final WsClient wsClient;

  /// Called when the unread message count changes.
  final ValueChanged<int>? onUnreadChanged;

  const WorkspaceChat({
    super.key,
    required this.wsClient,
    this.onUnreadChanged,
  });

  @override
  State<WorkspaceChat> createState() => WorkspaceChatState();
}

@visibleForTesting
class WorkspaceChatState extends State<WorkspaceChat> {
  final List<Map<String, dynamic>> _messages = [];
  final _scrollController = ScrollController();
  final _textController = TextEditingController();
  StreamSubscription<Map<String, dynamic>>? _chatSub;
  int _unreadCount = 0;
  bool _isVisible = false;

  @override
  void initState() {
    super.initState();
    _chatSub = widget.wsClient.chatMessages.listen(_onMessage);
  }

  /// Called by the parent when this tab becomes visible/hidden.
  void setVisible(bool visible) {
    _isVisible = visible;
    if (visible && _unreadCount > 0) {
      _unreadCount = 0;
      widget.onUnreadChanged?.call(0);
    }
  }

  void _onMessage(Map<String, dynamic> msg) {
    if (!mounted) return;
    final type = msg['type'] as String?;

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

    if (!_isVisible) {
      _unreadCount++;
      widget.onUnreadChanged?.call(_unreadCount);
    }

    // Auto-scroll to bottom after frame renders
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 150),
          curve: Curves.easeOut,
        );
      }
    });
  }

  static String _formatTime(String raw) {
    if (raw.isEmpty) return '';
    try {
      // Backend sends UTC datetime as "YYYY-MM-DD HH:MM:SS"
      final utc = DateTime.parse('${raw}Z');
      final local = utc.toLocal();
      final now = DateTime.now();
      final diff = now.difference(local);

      final hh = local.hour.toString().padLeft(2, '0');
      final mm = local.minute.toString().padLeft(2, '0');
      final time = '$hh:$mm';

      if (diff.inDays == 0 && local.day == now.day) {
        return time; // today: "14:30"
      }
      if (diff.inDays < 7) {
        const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        return '${days[local.weekday - 1]} $time'; // this week: "Mon 14:30"
      }
      return '${local.month}/${local.day} $time'; // older: "6/3 14:30"
    } catch (_) {
      return raw;
    }
  }

  static Color _colorForEmail(String email) {
    // Generate a stable, visually distinct color from the email hash.
    // Use HSL with fixed saturation/lightness for readability on dark bg.
    final hash = email.hashCode & 0x7fffffff;
    final hue = (hash % 360).toDouble();
    return HSLColor.fromAHSL(1.0, hue, 0.6, 0.7).toColor();
  }

  static final _urlRegex = RegExp(r'https?://[^\s<>"{}|\\^`\[\]]+');

  /// Build TextSpans for a message, turning URLs into clickable links.
  List<TextSpan> _buildMessageSpans(String text, bool isDeleted) {
    final style = TextStyle(
      color: isDeleted ? KColors.textMuted : KColors.textPrimary,
      fontSize: 13,
      fontStyle: isDeleted ? FontStyle.italic : FontStyle.normal,
    );

    if (isDeleted) {
      return [TextSpan(text: text, style: style)];
    }

    final spans = <TextSpan>[];
    int lastEnd = 0;
    for (final match in _urlRegex.allMatches(text)) {
      if (match.start > lastEnd) {
        spans.add(TextSpan(
          text: text.substring(lastEnd, match.start),
          style: style,
        ));
      }
      final url = match.group(0)!;
      spans.add(TextSpan(
        text: url,
        style: style.copyWith(
          color: KColors.accentBlue,
          decoration: TextDecoration.underline,
        ),
        recognizer: TapGestureRecognizer()
          ..onTap = () => openUrl(url), // coverage:ignore-line
      ));
      lastEnd = match.end;
    }
    if (lastEnd < text.length) {
      spans.add(TextSpan(
        text: text.substring(lastEnd),
        style: style,
      ));
    }
    // coverage:ignore-start
    if (spans.isEmpty) {
      spans.add(TextSpan(text: text, style: style));
    }
    // coverage:ignore-end
    return spans;
  }

  void _sendMessage() {
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    widget.wsClient.sendChatMessage(text);
    _textController.clear();
  }

  void _deleteMessage(String messageId) {
    widget.wsClient.sendChatDelete(messageId);
  }

  @override
  void dispose() {
    _chatSub?.cancel();
    _scrollController.dispose();
    _textController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final currentUserId = context.read<AuthService>().userId;

    return Container(
      color: KColors.bgCanvas,
      child: Column(
        children: [
          Expanded(
            child: _messages.isEmpty
                ? const Center(
                    child: Text(
                      'No messages yet',
                      style: TextStyle(color: KColors.textMuted),
                    ),
                  )
                : ListView.builder(
                    controller: _scrollController,
                    padding:
                        const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                    itemCount: _messages.length,
                    itemBuilder: (context, index) {
                      final msg = _messages[index];
                      final email = msg['user_email'] as String? ?? '';
                      final text = msg['message'] as String? ?? '';
                      final createdAt =
                          _formatTime(msg['created_at'] as String? ?? '');
                      final msgUserId = msg['user_id'] as String?;
                      final isOwn = msgUserId == currentUserId;
                      final isDeleted = text == '<message deleted by author>';
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Expanded(
                              child: SelectableText.rich(
                                TextSpan(
                                  children: [
                                    TextSpan(
                                      text: '$email  ',
                                      style: TextStyle(
                                        fontWeight: FontWeight.bold,
                                        color: _colorForEmail(email),
                                        fontSize: 13,
                                      ),
                                    ),
                                    ..._buildMessageSpans(text, isDeleted),
                                  ],
                                ),
                              ),
                            ),
                            const SizedBox(width: 8),
                            Text(
                              createdAt,
                              style: const TextStyle(
                                color: KColors.textMuted,
                                fontSize: 11,
                              ),
                            ),
                            if (isOwn && !isDeleted)
                              GestureDetector(
                                onTap: () =>
                                    _deleteMessage(msg['id'] as String),
                                child: const Padding(
                                  padding: EdgeInsets.only(left: 4),
                                  child: Icon(
                                    Icons.close,
                                    size: 14,
                                    color: KColors.textMuted,
                                  ),
                                ),
                              ),
                          ],
                        ),
                      );
                    },
                  ),
          ),
          Container(
            decoration: const BoxDecoration(
              border: Border(
                top: BorderSide(color: KColors.borderDefault),
              ),
            ),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _textController,
                    style: const TextStyle(
                      color: KColors.textPrimary,
                      fontSize: 13,
                    ),
                    decoration: const InputDecoration(
                      hintText: 'Type a message...',
                      hintStyle: TextStyle(color: KColors.textMuted),
                      border: InputBorder.none,
                      isDense: true,
                      contentPadding:
                          EdgeInsets.symmetric(vertical: 8, horizontal: 8),
                    ),
                    onSubmitted: (_) => _sendMessage(),
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.send, size: 18),
                  color: KColors.accentBlue,
                  onPressed: _sendMessage,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
