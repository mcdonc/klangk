import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:markdown/markdown.dart' as md;
import '../theme/colors.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// Scrollable list of chat messages with load-more and collapsible long
/// messages.
class ChatMessageList extends StatelessWidget {
  final List<Map<String, dynamic>> messages;
  final ScrollController scrollController;
  final String? currentUserId;
  final bool loadingOlder;
  final Set<String> expandedMessages;
  final ValueChanged<String> onToggleExpand;
  final ValueChanged<String> onDelete;

  const ChatMessageList({
    super.key,
    required this.messages,
    required this.scrollController,
    this.currentUserId,
    this.loadingOlder = false,
    required this.expandedMessages,
    required this.onToggleExpand,
    required this.onDelete,
  });

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: messages.isEmpty
          ? const Center(
              child: Text(
                'No messages yet',
                style: TextStyle(color: KColors.textMuted),
              ),
            )
          : ListView.builder(
              controller: scrollController,
              padding: const EdgeInsets.symmetric(
                horizontal: 12,
                vertical: 8,
              ),
              itemCount: messages.length + (loadingOlder ? 1 : 0),
              itemBuilder: (context, index) {
                if (loadingOlder && index == 0) {
                  return const Padding(
                    padding: EdgeInsets.symmetric(vertical: 8),
                    child: Center(
                      child: SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                    ),
                  );
                }
                final msgIndex = loadingOlder ? index - 1 : index;
                return _buildMessageItem(context, messages[msgIndex]);
              },
            ),
    );
  }

  Widget _buildMessageItem(BuildContext context, Map<String, dynamic> msg) {
    final handle = msg['user_handle'] as String?;
    final email = msg['user_email'] as String? ?? '';
    final senderName = (handle != null && handle.isNotEmpty) ? handle : email;
    final text = msg['message'] as String? ?? '';
    final createdAt = formatTime(msg['created_at'] as String? ?? '');
    final msgUserId = msg['user_id'] as String?;
    final isOwn = msgUserId != null && msgUserId == currentUserId;
    final isDeleted = text == '<message deleted by author>';
    final messageType = msg['message_type'] as int? ?? 0;

    if (messageType == 2 && isOwn) {
      return const SizedBox.shrink();
    }

    if (messageType == 2) {
      return _buildSystemMessage(text);
    }

    return _buildMessageRow(
      context,
      msg: msg,
      senderName: senderName,
      text: text,
      createdAt: createdAt,
      isAgent: messageType == 1,
      isOwn: isOwn,
      isDeleted: isDeleted,
    );
  }

  Widget _buildSystemMessage(String text) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          const Expanded(
            child: Divider(color: KColors.borderMuted, height: 1),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            child: Text(
              text,
              style: const TextStyle(
                color: KColors.textMuted,
                fontSize: 10,
              ),
            ),
          ),
          const Expanded(
            child: Divider(color: KColors.borderMuted, height: 1),
          ),
        ],
      ),
    );
  }

  Widget _buildMessageRow(
    BuildContext context, {
    required Map<String, dynamic> msg,
    required String senderName,
    required String text,
    required String createdAt,
    required bool isAgent,
    required bool isOwn,
    required bool isDeleted,
  }) {
    return Container(
      margin: const EdgeInsets.only(bottom: 6),
      padding: isAgent ? const EdgeInsets.only(left: 8) : EdgeInsets.zero,
      decoration: isAgent
          ? const BoxDecoration(
              border: Border(
                left: BorderSide(color: KColors.accentCyan, width: 2),
              ),
            )
          : null,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (isAgent)
            const Padding(
              padding: EdgeInsets.only(right: 6, top: 1),
              child: Icon(
                Icons.smart_toy,
                size: 14,
                color: KColors.accentCyan,
              ),
            ),
          Expanded(
            child: _CollapsibleMessage(
              messageId: msg['id'] as String? ?? '',
              isExpanded: expandedMessages.contains(msg['id']),
              onToggle: () => onToggleExpand(msg['id'] as String? ?? ''),
              child: _buildMessageContent(
                context,
                senderName: senderName,
                text: text,
                isAgent: isAgent,
                isDeleted: isDeleted,
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
              onTap: () => onDelete(msg['id'] as String),
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
  }

  Widget _buildMessageContent(
    BuildContext context, {
    required String senderName,
    required String text,
    required bool isAgent,
    required bool isDeleted,
  }) {
    final nameColor =
        isAgent ? KColors.accentCyan : KColors.colorForString(senderName);

    if (isDeleted) {
      return Text.rich(
        TextSpan(
          children: [
            TextSpan(
              text: '$senderName  ',
              style: TextStyle(
                fontWeight: FontWeight.bold,
                color: nameColor,
                fontSize: 13,
              ),
            ),
            TextSpan(
              text: text,
              style: const TextStyle(
                color: KColors.textMuted,
                fontSize: 13,
                fontStyle: FontStyle.italic,
              ),
            ),
          ],
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          senderName,
          style: TextStyle(
            fontWeight: FontWeight.bold,
            color: nameColor,
            fontSize: 13,
          ),
        ),
        MarkdownBody(
          data: highlightMentions(text),
          selectable: true,
          extensionSet: md.ExtensionSet(
            md.ExtensionSet.gitHubWeb.blockSyntaxes,
            [
              ...md.ExtensionSet.gitHubWeb.inlineSyntaxes.where(
                (s) =>
                    s is! md.AutolinkSyntax && s is! md.AutolinkExtensionSyntax,
              ),
            ],
          ),
          styleSheet: chatMarkdownStyle(context),
          // coverage:ignore-start
          onTapLink: (text, href, title) {
            if (href != null && href.isNotEmpty) {
              openUrl(href);
            }
          },
          // coverage:ignore-end
        ),
      ],
    );
  }

  // --- Static helpers (public for testing) ---

  static String formatTime(String raw) {
    if (raw.isEmpty) return '';
    try {
      final utc = DateTime.parse('${raw}Z');
      final local = utc.toLocal();
      final now = DateTime.now();
      final diff = now.difference(local);

      final hh = local.hour.toString().padLeft(2, '0');
      final mm = local.minute.toString().padLeft(2, '0');
      final time = '$hh:$mm';

      if (diff.inDays == 0 && local.day == now.day) {
        return time;
      }
      if (diff.inDays < 7) {
        const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        return '${days[local.weekday - 1]} $time';
      }
      return '${local.month}/${local.day} $time';
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[ChatMessageList] format time failed: $e');
      return raw;
    } // coverage:ignore-end
  }

  static final _mentionRegex = RegExp(r'@(\S+)');

  static String highlightMentions(String text) {
    return text.replaceAllMapped(_mentionRegex, (m) {
      return '**${m.group(0)}**';
    });
  }

  static MarkdownStyleSheet chatMarkdownStyle(BuildContext context) {
    const baseText = TextStyle(color: KColors.textPrimary, fontSize: 13);
    return MarkdownStyleSheet(
      p: baseText,
      pPadding: EdgeInsets.zero,
      a: baseText.copyWith(
        color: KColors.accentBlue,
        decoration: TextDecoration.underline,
      ),
      strong: baseText.copyWith(fontWeight: FontWeight.bold),
      em: baseText.copyWith(fontStyle: FontStyle.italic),
      code: baseText.copyWith(
        backgroundColor: KColors.bgOverlay,
        fontFamily: 'JetBrains Mono',
        fontSize: 12,
      ),
      codeblockDecoration: BoxDecoration(
        color: KColors.bgOverlay,
        borderRadius: BorderRadius.circular(4),
      ),
      codeblockPadding: const EdgeInsets.all(8),
      blockSpacing: 4,
      listBullet: baseText,
      blockquoteDecoration: BoxDecoration(
        border: Border(
          left: BorderSide(color: KColors.borderDefault, width: 3),
        ),
      ),
      blockquotePadding: const EdgeInsets.only(left: 8),
      blockquote: baseText.copyWith(color: KColors.textSecondary),
    );
  }
}

/// Truncates long messages to ~3 lines with a "show more" / "show less" toggle.
class _CollapsibleMessage extends StatelessWidget {
  const _CollapsibleMessage({
    required this.messageId,
    required this.isExpanded,
    required this.onToggle,
    required this.child,
  });

  final String messageId;
  final bool isExpanded;
  final VoidCallback onToggle;
  final Widget child;

  static const _collapsedMaxHeight = 60.0;

  @override
  Widget build(BuildContext context) {
    if (isExpanded) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          child,
          ExcludeFocus(
            child: GestureDetector(
              onTap: onToggle,
              child: MouseRegion(
                cursor: SystemMouseCursors.click,
                child: const Padding(
                  padding: EdgeInsets.only(top: 2),
                  child: Text(
                    'show less',
                    style: TextStyle(color: KColors.accentBlue, fontSize: 12),
                  ),
                ),
              ),
            ),
          ),
        ],
      );
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        return _MeasuredCollapse(
          maxHeight: _collapsedMaxHeight,
          onToggle: onToggle,
          child: child,
        );
      },
    );
  }
}

/// Measures the child's height and shows a "show more" link if it overflows.
class _MeasuredCollapse extends StatefulWidget {
  const _MeasuredCollapse({
    required this.maxHeight,
    required this.onToggle,
    required this.child,
  });

  final double maxHeight;
  final VoidCallback onToggle;
  final Widget child;

  @override
  State<_MeasuredCollapse> createState() => _MeasuredCollapseState();
}

class _MeasuredCollapseState extends State<_MeasuredCollapse> {
  bool _overflows = false;
  final _childKey = GlobalKey();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _checkOverflow());
  }

  @override
  void didUpdateWidget(_MeasuredCollapse oldWidget) {
    super.didUpdateWidget(oldWidget);
    WidgetsBinding.instance.addPostFrameCallback((_) => _checkOverflow());
  }

  void _checkOverflow() {
    final renderBox =
        _childKey.currentContext?.findRenderObject() as RenderBox?;
    if (renderBox == null || !mounted) return;
    final childHeight = renderBox.size.height;
    final overflows = childHeight > widget.maxHeight;
    if (overflows != _overflows) {
      setState(() => _overflows = overflows);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (_overflows)
          SizedBox(
            height: widget.maxHeight,
            child: ClipRect(
              child: OverflowBox(
                alignment: Alignment.topLeft,
                maxHeight: double.infinity,
                child: KeyedSubtree(key: _childKey, child: widget.child),
              ),
            ),
          )
        else
          KeyedSubtree(key: _childKey, child: widget.child),
        if (_overflows)
          ExcludeFocus(
            child: GestureDetector(
              onTap: widget.onToggle,
              child: MouseRegion(
                cursor: SystemMouseCursors.click,
                child: const Padding(
                  padding: EdgeInsets.only(top: 2),
                  child: Text(
                    '…show more',
                    style: TextStyle(color: KColors.accentBlue, fontSize: 12),
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}
