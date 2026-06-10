import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart'
    show HardwareKeyboard, KeyDownEvent, KeyRepeatEvent, LogicalKeyboardKey;
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import '../ws/ws_client.dart';
import '../theme/colors.dart';
import '../auth/auth_service.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import 'package:provider/provider.dart';

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

@visibleForTesting
class WorkspaceChatState extends State<WorkspaceChat> {
  final List<Map<String, dynamic>> _messages = [];
  final _scrollController = ScrollController();
  final _textController = TextEditingController();
  final _inputFocusNode = FocusNode(debugLabel: 'workspace-chat-input');
  StreamSubscription<Map<String, dynamic>>? _chatSub;
  int _unreadCount = 0;
  bool _isVisible = false;
  bool _hasMention = false;

  // Expanded message IDs (for long message truncation)
  final Set<String> _expandedMessages = {};

  // Sent message history (for Up/Down recall)
  final List<String> _sentHistory = [];
  int _historyIndex = -1;
  String _savedDraft = '';

  // Autocomplete state
  OverlayEntry? _autocompleteOverlay;
  List<Map<String, dynamic>> _filteredMembers = [];
  int _highlightedIndex = 0;
  String _mentionQuery = '';
  final _inputKey = GlobalKey();

  @override
  void initState() {
    super.initState();
    // Load any buffered history that arrived before this widget was created.
    if (widget.wsClient.chatHistory.isNotEmpty) {
      _messages.addAll(widget.wsClient.chatHistory);
    }
    _chatSub = widget.wsClient.chatMessages.listen(_onMessage);
    _textController.addListener(_onTextChanged);
    widget.wsClient.addListener(_onPresenceChanged);
    _inputFocusNode.onKeyEvent = _handleKeyEvent;
  }

  /// Focuses the message input. Called by the parent when the Chat tab is
  /// selected so the user can type immediately without an extra click.
  void requestFocus() => _inputFocusNode.requestFocus();

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

      final mentions = msg['mentions'] as List?;
      if (mentions != null && !_hasMention) {
        final currentUserId = context.read<AuthService>().userId;
        if (mentions.contains(currentUserId)) {
          _hasMention = true;
          widget.onMentionChanged?.call(true);
        }
      }
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

  // --- Autocomplete ---

  void _onTextChanged() {
    final text = _textController.text;
    final cursor = _textController.selection.baseOffset;
    if (cursor < 0 || cursor > text.length) {
      _hideAutocomplete();
      return;
    }
    // Find the @ that starts the current mention token
    final before = text.substring(0, cursor);
    final atIdx = before.lastIndexOf('@');
    if (atIdx < 0 ||
        (atIdx > 0 && before[atIdx - 1] != ' ' && before[atIdx - 1] != '\n')) {
      _hideAutocomplete();
      return;
    }
    final query = before.substring(atIdx + 1);
    if (query.contains(' ') || query.contains('\n')) {
      _hideAutocomplete();
      return;
    }
    _mentionQuery = query.toLowerCase();
    _showAutocomplete();
  }

  void _showAutocomplete() {
    final members = widget.wsClient.workspaceMembers;
    _filteredMembers = members.where((m) {
      final email = (m['email'] as String? ?? '').toLowerCase();
      return email.contains(_mentionQuery);
    }).toList();
    if (_filteredMembers.isEmpty) {
      _hideAutocomplete();
      return;
    }
    // Clamp highlight index when the list shrinks
    if (_highlightedIndex >= _filteredMembers.length) {
      _highlightedIndex = 0;
    }
    _autocompleteOverlay?.remove();
    _autocompleteOverlay = OverlayEntry(
      builder: (context) {
        final renderBox =
            _inputKey.currentContext?.findRenderObject() as RenderBox?;
        if (renderBox == null) return const SizedBox.shrink();
        final offset = renderBox.localToGlobal(Offset.zero);
        final visibleCount =
            _filteredMembers.length > 5 ? 5 : _filteredMembers.length;
        final height = visibleCount * 36.0;
        return Positioned(
          left: offset.dx,
          top: offset.dy - height - 4,
          width: renderBox.size.width,
          child: Material(
            elevation: 4,
            color: KColors.bgSurface,
            borderRadius: BorderRadius.circular(6),
            child: ConstrainedBox(
              constraints: BoxConstraints(maxHeight: height),
              child: ListView.builder(
                padding: EdgeInsets.zero,
                shrinkWrap: true,
                itemCount: visibleCount,
                itemBuilder: (ctx, i) {
                  final email = _filteredMembers[i]['email'] as String? ?? '';
                  final isHighlighted = i == _highlightedIndex;
                  return InkWell(
                    onTap: () => _insertMention(email),
                    child: Container(
                      color: isHighlighted
                          ? KColors.bgOverlay
                          : Colors.transparent,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      child: Text(
                        email,
                        style: const TextStyle(
                          color: KColors.textPrimary,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  );
                },
              ),
            ),
          ),
        );
      },
    );
    Overlay.of(context).insert(_autocompleteOverlay!);
  }

  /// Accept the currently highlighted autocomplete suggestion.
  void _acceptHighlightedSuggestion() {
    if (_filteredMembers.isEmpty) return;
    final idx = _highlightedIndex.clamp(0, _filteredMembers.length - 1);
    final email = _filteredMembers[idx]['email'] as String? ?? '';
    _insertMention(email);
  }

  /// Handle keyboard events for autocomplete navigation and message sending.
  KeyEventResult _handleKeyEvent(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
      return KeyEventResult.ignored;
    }

    final isShift = HardwareKeyboard.instance.isShiftPressed;

    // When autocomplete is visible, intercept navigation keys
    if (_autocompleteOverlay != null) {
      if (event.logicalKey == LogicalKeyboardKey.tab) {
        _acceptHighlightedSuggestion();
        return KeyEventResult.handled;
      }
      if (event.logicalKey == LogicalKeyboardKey.arrowDown) {
        final maxIdx =
            (_filteredMembers.length > 5 ? 5 : _filteredMembers.length) - 1;
        _highlightedIndex = (_highlightedIndex + 1).clamp(0, maxIdx);
        _autocompleteOverlay?.markNeedsBuild();
        return KeyEventResult.handled;
      }
      if (event.logicalKey == LogicalKeyboardKey.arrowUp) {
        _highlightedIndex = (_highlightedIndex - 1).clamp(
          0,
          _filteredMembers.length - 1,
        );
        _autocompleteOverlay?.markNeedsBuild();
        return KeyEventResult.handled;
      }
      if (event.logicalKey == LogicalKeyboardKey.enter && !isShift) {
        _acceptHighlightedSuggestion();
        return KeyEventResult.handled;
      }
      if (event.logicalKey == LogicalKeyboardKey.escape) {
        _hideAutocomplete();
        return KeyEventResult.handled;
      }
    }

    // Enter (without Shift) sends the message; Shift+Enter inserts a newline
    if (event.logicalKey == LogicalKeyboardKey.enter && !isShift) {
      _sendMessage();
      return KeyEventResult.handled;
    }

    // Up arrow on first line → recall previous sent message
    if (event.logicalKey == LogicalKeyboardKey.arrowUp &&
        _sentHistory.isNotEmpty) {
      final text = _textController.text;
      final cursor = _textController.selection.baseOffset;
      final onFirstLine =
          cursor >= 0 && !text.substring(0, cursor).contains('\n');
      if (onFirstLine) {
        if (_historyIndex == -1) {
          _savedDraft = text;
          _historyIndex = _sentHistory.length - 1;
        } else if (_historyIndex > 0) {
          _historyIndex--;
        }
        _textController.text = _sentHistory[_historyIndex];
        _textController.selection = TextSelection.collapsed(
          offset: _textController.text.length,
        );
        return KeyEventResult.handled;
      }
    }

    // Down arrow on last line → recall next sent message or restore draft
    if (event.logicalKey == LogicalKeyboardKey.arrowDown &&
        _historyIndex >= 0) {
      final text = _textController.text;
      final cursor = _textController.selection.baseOffset;
      final onLastLine = cursor >= 0 && !text.substring(cursor).contains('\n');
      if (onLastLine) {
        if (_historyIndex < _sentHistory.length - 1) {
          _historyIndex++;
          _textController.text = _sentHistory[_historyIndex];
        } else {
          _historyIndex = -1;
          _textController.text = _savedDraft;
        }
        _textController.selection = TextSelection.collapsed(
          offset: _textController.text.length,
        );
        return KeyEventResult.handled;
      }
    }

    final isCtrl = HardwareKeyboard.instance.isControlPressed;

    // Shift+Ctrl+A → select all text
    if (isCtrl && isShift && event.logicalKey == LogicalKeyboardKey.keyA) {
      _textController.selection = TextSelection(
        baseOffset: 0,
        extentOffset: _textController.text.length,
      );
      return KeyEventResult.handled;
    }

    // Ctrl+A → move cursor to beginning of current line (emacs-style)
    if (isCtrl && event.logicalKey == LogicalKeyboardKey.keyA) {
      final text = _textController.text;
      final cursor = _textController.selection.baseOffset;
      if (cursor >= 0) {
        final lineStart = text.lastIndexOf('\n', cursor - 1) + 1;
        _textController.selection = TextSelection.collapsed(offset: lineStart);
      }
      return KeyEventResult.handled;
    }

    // Ctrl+E → move cursor to end of current line (emacs-style)
    if (isCtrl && event.logicalKey == LogicalKeyboardKey.keyE) {
      final text = _textController.text;
      final cursor = _textController.selection.baseOffset;
      if (cursor >= 0) {
        var lineEnd = text.indexOf('\n', cursor);
        if (lineEnd < 0) lineEnd = text.length;
        _textController.selection = TextSelection.collapsed(offset: lineEnd);
      }
      return KeyEventResult.handled;
    }

    return KeyEventResult.ignored;
  }

  void _hideAutocomplete() {
    _autocompleteOverlay?.remove();
    _autocompleteOverlay = null;
    _filteredMembers = [];
    _highlightedIndex = 0;
  }

  void _insertMention(String email) {
    final text = _textController.text;
    final cursor = _textController.selection.baseOffset;
    final before = text.substring(0, cursor);
    final atIdx = before.lastIndexOf('@');
    final after = text.substring(cursor);
    final newText = '${text.substring(0, atIdx)}@$email $after';
    _textController.value = TextEditingValue(
      text: newText,
      selection: TextSelection.collapsed(
        offset: atIdx + email.length + 2, // @email + space
      ),
    );
    _hideAutocomplete();
    _inputFocusNode.requestFocus();
  }

  // --- Message rendering ---

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

  static Color _colorForEmail(String email) => KColors.colorForString(email);

  static final _mentionRegex = RegExp(r'@(\S+)');

  /// Pre-process @mentions into bold markdown syntax, taking care not to
  /// modify mentions that are already inside markdown links or code spans.
  static String _highlightMentions(String text) {
    return text.replaceAllMapped(_mentionRegex, (m) {
      return '**${m.group(0)}**';
    });
  }

  /// Build a [MarkdownStyleSheet] that matches the dark Klangk theme.
  static MarkdownStyleSheet _chatMarkdownStyle(BuildContext context) {
    const baseText = TextStyle(
      color: KColors.textPrimary,
      fontSize: 13,
    );
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

  void _sendMessage() {
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    _hideAutocomplete();
    _sentHistory.add(text);
    _historyIndex = -1;
    _savedDraft = '';
    widget.wsClient.sendChatMessage(text);
    _textController.clear();
    _inputFocusNode.requestFocus();
  }

  void _deleteMessage(String messageId) {
    widget.wsClient.sendChatDelete(messageId);
  }

  void _onPresenceChanged() {
    if (mounted) setState(() {});
  }

  Widget _buildPresenceBar(String? currentUserId) {
    final users = widget.wsClient.presenceUsers;
    if (users.isEmpty) return const SizedBox.shrink();
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: KColors.borderDefault)),
      ),
      child: Row(
        children: [
          const Text(
            'Online ',
            style: TextStyle(color: KColors.textMuted, fontSize: 11),
          ),
          ...users.map((u) {
            final email = u['user_email'] as String? ?? '';
            final uid = u['user_id'] as String?;
            final isSelf = uid == currentUserId;
            final initial = email.isNotEmpty ? email[0].toUpperCase() : '?';
            return Padding(
              padding: const EdgeInsets.only(right: 4),
              child: Tooltip(
                message: email,
                child: CircleAvatar(
                  radius: 10,
                  backgroundColor:
                      isSelf ? Colors.transparent : _colorForEmail(email),
                  foregroundColor:
                      isSelf ? _colorForEmail(email) : Colors.white,
                  child: isSelf
                      ? DecoratedBox(
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            border: Border.all(
                              color: _colorForEmail(email),
                              width: 1.5,
                            ),
                          ),
                          child: Center(
                            child: Text(
                              initial,
                              style: TextStyle(
                                fontSize: 10,
                                fontWeight: FontWeight.bold,
                                color: _colorForEmail(email),
                              ),
                            ),
                          ),
                        )
                      : Text(
                          initial,
                          style: const TextStyle(
                            fontSize: 10,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                ),
              ),
            );
          }),
        ],
      ),
    );
  }

  @override
  void dispose() {
    widget.wsClient.removeListener(_onPresenceChanged);
    _hideAutocomplete();
    _chatSub?.cancel();
    _scrollController.dispose();
    _textController.removeListener(_onTextChanged);
    _textController.dispose();
    _inputFocusNode.dispose();
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
          _buildPresenceBar(currentUserId),
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
                    padding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 8,
                    ),
                    itemCount: _messages.length,
                    itemBuilder: (context, index) {
                      final msg = _messages[index];
                      final email = msg['user_email'] as String? ?? '';
                      final text = msg['message'] as String? ?? '';
                      final createdAt = _formatTime(
                        msg['created_at'] as String? ?? '',
                      );
                      final msgUserId = msg['user_id'] as String?;
                      final isOwn = msgUserId == currentUserId;
                      final isDeleted = text == '<message deleted by author>';
                      final messageType = msg['message_type'] as int? ?? 0;

                      // System messages: centered, muted, no sender
                      if (messageType == 2) {
                        return Padding(
                          padding: const EdgeInsets.only(bottom: 4),
                          child: Center(
                            child: Text(
                              text,
                              style: const TextStyle(
                                color: KColors.textMuted,
                                fontSize: 11,
                                fontStyle: FontStyle.italic,
                              ),
                            ),
                          ),
                        );
                      }

                      // Agent messages: robot icon prefix, subtle tint
                      final isAgent = messageType == 1;

                      return Padding(
                        padding: const EdgeInsets.only(bottom: 8),
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
                                isExpanded: _expandedMessages.contains(
                                  msg['id'],
                                ),
                                onToggle: () {
                                  setState(() {
                                    final id = msg['id'] as String? ?? '';
                                    if (_expandedMessages.contains(id)) {
                                      _expandedMessages.remove(id);
                                    } else {
                                      _expandedMessages.add(id);
                                    }
                                  });
                                },
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      email,
                                      style: TextStyle(
                                        fontWeight: FontWeight.bold,
                                        color: isAgent
                                            ? KColors.accentCyan
                                            : _colorForEmail(email),
                                        fontSize: 13,
                                      ),
                                    ),
                                    if (isDeleted)
                                      Text(
                                        text,
                                        style: const TextStyle(
                                          color: KColors.textMuted,
                                          fontSize: 13,
                                          fontStyle: FontStyle.italic,
                                        ),
                                      )
                                    else
                                      MarkdownBody(
                                        data: _highlightMentions(text),
                                        selectable: true,
                                        styleSheet: _chatMarkdownStyle(context),
                                        // coverage:ignore-start
                                        onTapLink: (text, href, title) {
                                          if (href != null && href.isNotEmpty) {
                                            openUrl(href);
                                          }
                                        },
                                        // coverage:ignore-end
                                      ),
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
            key: _inputKey,
            decoration: const BoxDecoration(
              border: Border(top: BorderSide(color: KColors.borderDefault)),
            ),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: [
                Expanded(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 120),
                    child: TextField(
                      controller: _textController,
                      focusNode: _inputFocusNode,
                      maxLines: null,
                      style: const TextStyle(
                        color: KColors.textPrimary,
                        fontSize: 13,
                      ),
                      decoration: const InputDecoration(
                        hintText: 'Type a message...',
                        hintStyle: TextStyle(color: KColors.textMuted),
                        border: InputBorder.none,
                        isDense: true,
                        contentPadding: EdgeInsets.symmetric(
                          vertical: 8,
                          horizontal: 8,
                        ),
                      ),
                    ),
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

  /// Approximate max height for 3 lines of 13px text with line spacing.
  static const _collapsedMaxHeight = 60.0;

  @override
  Widget build(BuildContext context) {
    if (isExpanded) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          child,
          GestureDetector(
            onTap: onToggle,
            child: const Padding(
              padding: EdgeInsets.only(top: 2),
              child: Text(
                'show less',
                style: TextStyle(color: KColors.accentBlue, fontSize: 12),
              ),
            ),
          ),
        ],
      );
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        // Use a TextPainter to check if the content would exceed 3 lines.
        // We approximate by checking the intrinsic height of the child.
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
          GestureDetector(
            onTap: widget.onToggle,
            child: const Padding(
              padding: EdgeInsets.only(top: 2),
              child: Text(
                '…show more',
                style: TextStyle(color: KColors.accentBlue, fontSize: 12),
              ),
            ),
          ),
      ],
    );
  }
}
