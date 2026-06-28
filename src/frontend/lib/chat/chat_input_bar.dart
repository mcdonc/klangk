import 'package:flutter/material.dart';
import 'package:flutter/services.dart'
    show HardwareKeyboard, KeyDownEvent, KeyRepeatEvent, LogicalKeyboardKey;
import '../theme/colors.dart';

/// Chat text input with send/abort buttons, @mention autocomplete,
/// emacs keybindings, and sent-message history recall.
class ChatInputBar extends StatefulWidget {
  final ValueChanged<String> onSendText;
  final bool agentThinking;
  final VoidCallback? onAbort;
  final List<Map<String, dynamic>> members;

  const ChatInputBar({
    super.key,
    required this.onSendText,
    this.agentThinking = false,
    this.onAbort,
    this.members = const [],
  });

  @override
  State<ChatInputBar> createState() => ChatInputBarState();
}

class ChatInputBarState extends State<ChatInputBar> {
  final _textController = TextEditingController();
  final _inputFocusNode = FocusNode(debugLabel: 'workspace-chat-input');
  final _inputKey = GlobalKey();

  // Sent message history (for Up/Down recall)
  final List<String> _sentHistory = [];
  int _historyIndex = -1;
  String _savedDraft = '';

  // Autocomplete state
  OverlayEntry? _autocompleteOverlay;
  List<Map<String, dynamic>> _filteredMembers = [];
  int _highlightedIndex = 0;
  String _mentionQuery = '';

  @override
  void initState() {
    super.initState();
    _textController.addListener(_onTextChanged);
    _inputFocusNode.onKeyEvent = _handleKeyEvent;
  }

  /// Focuses the message input.
  void requestFocus() => _inputFocusNode.requestFocus();

  // --- Autocomplete ---

  void _onTextChanged() {
    final text = _textController.text;
    final cursor = _textController.selection.baseOffset;
    if (cursor < 0 || cursor > text.length) {
      _hideAutocomplete();
      return;
    }
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
    _filteredMembers = widget.members.where((m) {
      final email = (m['email'] as String? ?? '').toLowerCase();
      final handle = (m['handle'] as String? ?? '').toLowerCase();
      return handle.contains(_mentionQuery) || email.contains(_mentionQuery);
    }).toList();
    if (_filteredMembers.isEmpty) {
      _hideAutocomplete();
      return;
    }
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
                  final handle = _filteredMembers[i]['handle'] as String? ?? '';
                  final email = _filteredMembers[i]['email'] as String? ?? '';
                  final displayName = handle.isNotEmpty ? handle : email;
                  final isHighlighted = i == _highlightedIndex;
                  return InkWell(
                    onTap: () => _insertMention(displayName),
                    child: Container(
                      color: isHighlighted
                          ? KColors.bgOverlay
                          : Colors.transparent,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      child: Text(
                        displayName,
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

  void _acceptHighlightedSuggestion() {
    if (_filteredMembers.isEmpty) return;
    final idx = _highlightedIndex.clamp(0, _filteredMembers.length - 1);
    final handle = _filteredMembers[idx]['handle'] as String? ?? '';
    final email = _filteredMembers[idx]['email'] as String? ?? '';
    _insertMention(handle.isNotEmpty ? handle : email);
  }

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

    // Ctrl+K → kill from cursor to end of current line (emacs-style)
    if (isCtrl && event.logicalKey == LogicalKeyboardKey.keyK) {
      final text = _textController.text;
      final cursor = _textController.selection.baseOffset;
      if (cursor >= 0) {
        var lineEnd = text.indexOf('\n', cursor);
        if (lineEnd < 0) {
          lineEnd = text.length;
        } else if (lineEnd == cursor) {
          lineEnd = cursor + 1;
        }
        final newText = text.substring(0, cursor) + text.substring(lineEnd);
        _textController.value = TextEditingValue(
          text: newText,
          selection: TextSelection.collapsed(offset: cursor),
        );
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
        offset: atIdx + email.length + 2,
      ),
    );
    _hideAutocomplete();
    _inputFocusNode.requestFocus();
  }

  void _sendMessage() {
    final text = _textController.text.trim();
    if (text.isEmpty) return;
    _hideAutocomplete();
    _sentHistory.add(text);
    _historyIndex = -1;
    _savedDraft = '';
    widget.onSendText(text);
    _textController.clear();
    _inputFocusNode.requestFocus();
  }

  @override
  void dispose() {
    _hideAutocomplete();
    _textController.removeListener(_onTextChanged);
    _textController.dispose();
    _inputFocusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
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
          if (widget.agentThinking)
            IconButton(
              icon: const Icon(Icons.stop_circle_outlined, size: 18),
              color: KColors.accentRed,
              tooltip: 'Stop agent',
              onPressed: widget.onAbort,
            ),
          IconButton(
            icon: const Icon(Icons.send, size: 18),
            color: KColors.accentBlue,
            onPressed: _sendMessage,
          ),
        ],
      ),
    );
  }
}
