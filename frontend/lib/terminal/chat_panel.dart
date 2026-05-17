import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_highlight/themes/monokai-sublime.dart';
import 'package:highlight/highlight.dart' show highlight, Node;
import 'package:http/http.dart' as http;
import '../utils/backend_url.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.html) '../utils/web_helpers_web.dart';
import '../agui/agui_client.dart';
import '../agui/agui_events.dart';

/// Chat-style panel with markdown rendering for assistant responses.
class ChatPanel extends StatefulWidget {
  final AguiClient aguiClient;
  final String? workspaceId;
  final String? authToken;

  const ChatPanel(
      {super.key, required this.aguiClient, this.workspaceId, this.authToken});

  @override
  State<ChatPanel> createState() => _ChatPanelState();
}

class _ChatPanelState extends State<ChatPanel> {
  final List<_ChatEntry> _entries = [];
  final _scrollController = ScrollController();
  final _inputController = TextEditingController();
  final _inputFocus = FocusNode();
  late final StreamSubscription<AguiEvent> _eventSub;
  bool _agentRunning = false;

  // Input history navigation
  int _historyIndex = -1;
  String _savedInput = '';

  // Buffer for streaming assistant text
  String _currentAssistantText = '';
  String? _currentMessageId;

  @override
  void initState() {
    super.initState();
    _eventSub = widget.aguiClient.events.listen(_handleEvent);
    _loadHistory();
  }

  Future<void> _loadHistory() async {
    if (widget.workspaceId == null || widget.authToken == null) return;
    try {
      final response = await http.get(
        Uri.parse('$baseUrl/workspaces/${widget.workspaceId}/messages'),
        headers: {'Authorization': 'Bearer ${widget.authToken}'},
      );
      if (response.statusCode == 200) {
        final messages = jsonDecode(response.body) as List;
        final entries = <_ChatEntry>[];
        for (final msg in messages) {
          final type = msg['entry_type'] as String;
          final content = msg['content'] as String? ?? '';
          if (type == 'user') {
            entries.add(_ChatEntry(
                type: _EntryType.user,
                content: content,
                isQueued: msg['is_queued'] as bool? ?? false));
          } else if (type == 'assistant') {
            entries
                .add(_ChatEntry(type: _EntryType.assistant, content: content));
          } else if (type == 'tool_call') {
            entries.add(_ChatEntry(
              type: _EntryType.toolCall,
              content: content,
              toolArgs: msg['tool_args'] as String?,
              toolOutput: msg['tool_output'] as String?,
              isComplete: msg['is_complete'] as bool? ?? true,
            ));
          } else if (type == 'error') {
            entries.add(_ChatEntry(type: _EntryType.error, content: content));
          }
        }
        if (mounted && entries.isNotEmpty) {
          setState(() => _entries.addAll(entries));
          // Delay scroll to allow layout to fully settle after loading history
          Future.delayed(const Duration(milliseconds: 200), _scrollToBottom);
        }
      }
    } catch (_) {}
  }

  void _handleEvent(AguiEvent event) {
    switch (event.type) {
      case AguiEventType.runStarted:
        setState(() => _agentRunning = true);
        break;

      case AguiEventType.runFinished:
        _finalizeAssistantMessage();
        setState(() {
          _agentRunning = false;
          _unqueueEntries();
        });
        break;

      case AguiEventType.runError:
        _finalizeAssistantMessage();
        setState(() {
          _agentRunning = false;
          _entries.add(_ChatEntry(
            type: _EntryType.error,
            content: event.message ?? 'Unknown error',
          ));
        });
        _scrollToBottom();
        break;

      case AguiEventType.textMessageStart:
        _currentMessageId = event.messageId;
        _currentAssistantText = '';
        break;

      case AguiEventType.textMessageContent:
        final delta = event.delta;
        if (delta != null) {
          _currentAssistantText += delta;
          // Update or add streaming entry
          setState(() {
            if (_entries.isNotEmpty &&
                _entries.last.type == _EntryType.assistantStreaming) {
              _entries[_entries.length - 1] = _ChatEntry(
                type: _EntryType.assistantStreaming,
                content: _currentAssistantText,
              );
            } else {
              _entries.add(_ChatEntry(
                type: _EntryType.assistantStreaming,
                content: _currentAssistantText,
              ));
            }
          });
          _scrollToBottom();
        }
        break;

      case AguiEventType.textMessageEnd:
        _finalizeAssistantMessage();
        break;

      case AguiEventType.toolCallStart:
        final name = event.toolCallName ?? 'tool';
        final args = event.toolCallArgs ?? '';
        setState(() {
          _entries.add(_ChatEntry(
            type: _EntryType.toolCall,
            content: name,
            toolArgs: args,
            toolOutput: '',
          ));
        });
        _scrollToBottom();
        break;

      case AguiEventType.toolCallArgs:
        final delta = event.delta;
        if (delta != null && delta.isNotEmpty) {
          _updateLastToolCall((last) => _ChatEntry(
                type: _EntryType.toolCall,
                content: last.content,
                toolArgs: last.toolArgs,
                toolOutput: (last.toolOutput ?? '') + delta,
              ));
        }
        break;

      case AguiEventType.toolCallResult:
        final content = event.content;
        if (content != null && content.isNotEmpty) {
          _updateLastToolCall((last) => _ChatEntry(
                type: _EntryType.toolCall,
                content: last.content,
                toolArgs: last.toolArgs,
                toolOutput: content.toString(),
                isComplete: true,
              ));
        }
        _scrollToBottom();
        break;

      case AguiEventType.custom:
        if (event.customName == 'prompt_queued') {
          // Mark the last user entry as queued
          setState(() {
            for (int i = _entries.length - 1; i >= 0; i--) {
              if (_entries[i].type == _EntryType.user &&
                  !_entries[i].isQueued) {
                _entries[i] = _ChatEntry(
                  type: _EntryType.user,
                  content: _entries[i].content,
                  isQueued: true,
                );
                break;
              }
            }
          });
        }
        break;
    }
  }

  void _unqueueEntries() {
    // No-op: we keep isQueued true for the label, but dimming is controlled by _agentRunning
  }

  void _finalizeAssistantMessage() {
    if (_currentAssistantText.isNotEmpty) {
      setState(() {
        // Replace streaming entry with final
        if (_entries.isNotEmpty &&
            _entries.last.type == _EntryType.assistantStreaming) {
          _entries[_entries.length - 1] = _ChatEntry(
            type: _EntryType.assistant,
            content: _currentAssistantText,
          );
        }
      });
      _currentAssistantText = '';
      _currentMessageId = null;
      _scrollToBottom();
    }
  }

  void _updateLastToolCall(_ChatEntry Function(_ChatEntry last) updater) {
    if (_entries.isNotEmpty && _entries.last.type == _EntryType.toolCall) {
      setState(() {
        _entries[_entries.length - 1] = updater(_entries.last);
      });
    }
  }

  List<String> get _userHistory {
    return _entries
        .where((e) => e.type == _EntryType.user)
        .map((e) => e.content)
        .toList();
  }

  void _navigateHistory(int direction) {
    final history = _userHistory;
    if (history.isEmpty) return;

    final text = _inputController.text;
    final selection = _inputController.selection;

    if (direction < 0) {
      // Up arrow: only if cursor is on the first line
      final textBeforeCursor = text.substring(0, selection.baseOffset);
      if (textBeforeCursor.contains('\n')) return; // not at top

      if (_historyIndex == -1) {
        _savedInput = text;
        _historyIndex = history.length - 1;
      } else if (_historyIndex > 0) {
        _historyIndex--;
      } else {
        return;
      }
    } else {
      // Down arrow: only if cursor is on the last line
      final textAfterCursor = text.substring(selection.baseOffset);
      if (textAfterCursor.contains('\n')) return; // not at bottom

      if (_historyIndex == -1) return;
      if (_historyIndex < history.length - 1) {
        _historyIndex++;
      } else {
        // Back to saved input
        _inputController.text = _savedInput;
        _inputController.selection =
            TextSelection.collapsed(offset: _savedInput.length);
        _historyIndex = -1;
        return;
      }
    }

    _inputController.text = history[_historyIndex];
    _inputController.selection =
        TextSelection.collapsed(offset: history[_historyIndex].length);
  }

  void _sendPrompt() {
    final text = _inputController.text.trim();
    if (text.isEmpty) return;

    _historyIndex = -1;
    _savedInput = '';

    setState(() {
      _entries.add(_ChatEntry(type: _EntryType.user, content: text));
    });
    widget.aguiClient.sendPrompt(text);
    _inputController.clear();
    _inputFocus.requestFocus();
    _scrollToBottom();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 100),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  void dispose() {
    _eventSub.cancel();
    _inputController.dispose();
    _inputFocus.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      children: [
        // Messages area
        Expanded(
          child: _entries.isEmpty
              ? Center(
                  child: Text(
                    'Type a message to start',
                    style: theme.textTheme.bodyLarge?.copyWith(
                      color: theme.colorScheme.onSurface.withOpacity(0.5),
                    ),
                  ),
                )
              : ListView.builder(
                  controller: _scrollController,
                  padding: const EdgeInsets.all(12),
                  itemCount: _entries.length,
                  itemBuilder: (context, index) => _buildEntry(_entries[index]),
                ),
        ),
        // Input area
        Container(
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(
            border: Border(
                top: BorderSide(color: theme.colorScheme.outlineVariant)),
          ),
          child: Row(
            children: [
              Expanded(
                child: KeyboardListener(
                  focusNode: FocusNode(),
                  onKeyEvent: (event) {
                    if (event is KeyDownEvent &&
                        event.logicalKey == LogicalKeyboardKey.enter &&
                        !HardwareKeyboard.instance.isShiftPressed) {
                      _sendPrompt();
                    } else if (event is KeyDownEvent &&
                        event.logicalKey == LogicalKeyboardKey.arrowUp) {
                      _navigateHistory(-1);
                    } else if (event is KeyDownEvent &&
                        event.logicalKey == LogicalKeyboardKey.arrowDown) {
                      _navigateHistory(1);
                    }
                  },
                  child: TextField(
                    controller: _inputController,
                    focusNode: _inputFocus,
                    autofocus: true,
                    style: const TextStyle(fontSize: 16),
                    decoration: InputDecoration(
                      hintText: _agentRunning
                          ? 'Agent is thinking...'
                          : 'Type a message...',
                      border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(8)),
                      contentPadding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 10),
                      isDense: true,
                    ),
                    maxLines: null,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              if (_agentRunning)
                IconButton(
                  icon: const Icon(Icons.stop_circle, color: Colors.red),
                  tooltip: 'Abort',
                  onPressed: widget.aguiClient.sendAbort,
                )
              else
                IconButton(
                  icon: Icon(Icons.send, color: theme.colorScheme.primary),
                  tooltip: 'Send',
                  onPressed: _sendPrompt,
                ),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildEntry(_ChatEntry entry) {
    return switch (entry.type) {
      _EntryType.user => _buildUserMessage(entry),
      _EntryType.assistant ||
      _EntryType.assistantStreaming =>
        _buildAssistantMessage(entry),
      _EntryType.toolCall => _buildToolCall(entry),
      _EntryType.error => _buildError(entry),
    };
  }

  Widget _buildUserMessage(_ChatEntry entry) {
    final opacity = (entry.isQueued && _agentRunning) ? 0.5 : 1.0;
    return Opacity(
      opacity: opacity,
      child: Align(
        alignment: Alignment.centerRight,
        child: Container(
          constraints: BoxConstraints(
              maxWidth: MediaQuery.of(context).size.width * 0.35),
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.primary.withOpacity(0.2),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              SelectableText(
                entry.content,
                style: const TextStyle(fontSize: 16),
              ),
              if (entry.isQueued)
                const Text('queued',
                    style: TextStyle(fontSize: 10, color: Colors.grey)),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildAssistantMessage(_ChatEntry entry) {
    final isStreaming = entry.type == _EntryType.assistantStreaming;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Theme.of(context).colorScheme.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          MarkdownBody(
            data: entry.content,
            selectable: true,
            onTapLink: (text, href, title) {
              if (href != null) {
                openUrl(href);
              }
            },
            syntaxHighlighter: _MonokaiSyntaxHighlighter(),
            styleSheet: MarkdownStyleSheet(
              a: const TextStyle(
                  fontSize: 16,
                  color: Color(0xFF1565C0),
                  decoration: TextDecoration.underline),
              p: const TextStyle(fontSize: 16, height: 1.5),
              code: TextStyle(
                fontSize: 15,
                fontFamily: 'JetBrains Mono',
                color: const Color(0xFFD63384),
                backgroundColor: const Color(0xFFF0F0F0),
              ),
              codeblockDecoration: BoxDecoration(
                color: const Color(0xFF272822),
                borderRadius: BorderRadius.circular(8),
              ),
              codeblockPadding: const EdgeInsets.all(12),
            ),
          ),
          if (isStreaming)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  color: Theme.of(context).colorScheme.primary,
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildToolCall(_ChatEntry entry) {
    final hasArgs = entry.toolArgs != null && entry.toolArgs!.isNotEmpty;
    final hasOutput = entry.toolOutput != null && entry.toolOutput!.isNotEmpty;
    final subtitle = hasArgs ? entry.toolArgs! : null;

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      decoration: BoxDecoration(
        border: Border.all(color: Colors.cyan.withOpacity(0.3)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: ExpansionTile(
        dense: true,
        initiallyExpanded: false,
        tilePadding: const EdgeInsets.symmetric(horizontal: 12),
        leading: Icon(
          entry.isComplete ? Icons.check_circle : Icons.play_circle,
          size: 18,
          color: entry.isComplete ? Colors.green : Colors.cyan,
        ),
        title: Text(
          entry.content,
          style: const TextStyle(
              fontSize: 13,
              fontWeight: FontWeight.bold,
              fontFamily: 'JetBrains Mono'),
        ),
        subtitle: subtitle != null
            ? Text(
                subtitle.length > 80
                    ? '${subtitle.substring(0, 80)}...'
                    : subtitle,
                style: TextStyle(
                    fontSize: 12,
                    fontFamily: 'JetBrains Mono',
                    color: Colors.grey[400]),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              )
            : null,
        children: [
          if (hasArgs)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
              color: Theme.of(context).colorScheme.surface,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Arguments',
                      style: TextStyle(
                          fontSize: 11,
                          color: Colors.grey[500],
                          fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
                  SelectableText(
                    entry.toolArgs!,
                    style: const TextStyle(
                        fontSize: 12, fontFamily: 'JetBrains Mono'),
                  ),
                ],
              ),
            ),
          if (hasOutput)
            Container(
              width: double.infinity,
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
              color: Theme.of(context).colorScheme.surface,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Result',
                      style: TextStyle(
                          fontSize: 11,
                          color: Colors.grey[500],
                          fontWeight: FontWeight.bold)),
                  const SizedBox(height: 4),
                  SelectableText(
                    entry.toolOutput!.length > 2000
                        ? '${entry.toolOutput!.substring(0, 2000)}...'
                        : entry.toolOutput!,
                    style: const TextStyle(
                        fontSize: 12, fontFamily: 'JetBrains Mono'),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _buildError(_ChatEntry entry) {
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: Colors.red.withOpacity(0.1),
        border: Border.all(color: Colors.red.withOpacity(0.3)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline, color: Colors.red, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: SelectableText(
              entry.content,
              style: const TextStyle(fontSize: 13, color: Colors.red),
            ),
          ),
        ],
      ),
    );
  }
}

enum _EntryType { user, assistant, assistantStreaming, toolCall, error }

class _ChatEntry {
  final _EntryType type;
  final String content;
  final String? toolArgs;
  final String? toolOutput;
  final bool isComplete;
  final bool isQueued;

  _ChatEntry({
    required this.type,
    required this.content,
    this.toolArgs,
    this.toolOutput,
    this.isQueued = false,
    this.isComplete = false,
  });
}

/// Syntax highlighter using highlight.dart with Monokai Sublime theme.
class _MonokaiSyntaxHighlighter extends SyntaxHighlighter {
  static const _defaultStyle = TextStyle(
    fontFamily: 'JetBrains Mono',
    fontSize: 15,
    color: Color(0xFFF8F8F2),
  );

  @override
  TextSpan format(String source) {
    final result = highlight.parse(source, autoDetection: true);
    return TextSpan(
      style: _defaultStyle,
      children: _buildSpans(result.nodes ?? []),
    );
  }

  List<TextSpan> _buildSpans(List<Node> nodes) {
    final spans = <TextSpan>[];
    for (final node in nodes) {
      if (node.value != null) {
        spans.add(TextSpan(
          text: node.value,
          style: _styleForClass(node.className),
        ));
      } else if (node.children != null) {
        spans.add(TextSpan(
          style: _styleForClass(node.className),
          children: _buildSpans(node.children!),
        ));
      }
    }
    return spans;
  }

  TextStyle? _styleForClass(String? className) {
    if (className == null) return null;
    final themeEntry = monokaiSublimeTheme[className];
    if (themeEntry == null) return null;
    return TextStyle(
      color: themeEntry.color,
      fontWeight: themeEntry.fontWeight,
      fontStyle: themeEntry.fontStyle,
    );
  }
}
