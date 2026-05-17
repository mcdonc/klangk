import 'dart:async';
import 'package:flutter/material.dart';
import '../agui/agui_client.dart';
import '../agui/agui_events.dart';

/// Structured output panel showing tool calls, errors, and reasoning.
class OutputPanel extends StatefulWidget {
  final AguiClient aguiClient;

  const OutputPanel({super.key, required this.aguiClient});

  @override
  State<OutputPanel> createState() => _OutputPanelState();
}

class _OutputPanelState extends State<OutputPanel> {
  final List<_OutputEntry> _entries = [];
  final _scrollController = ScrollController();
  late final StreamSubscription<AguiEvent> _eventSub;

  @override
  void initState() {
    super.initState();
    _eventSub = widget.aguiClient.events.listen(_handleEvent);
  }

  void _handleEvent(AguiEvent event) {
    _OutputEntry? entry;

    switch (event.type) {
      case AguiEventType.toolCallStart:
        entry = _OutputEntry(
          type: _EntryType.toolCall,
          title: event.toolCallName ?? 'tool',
          content: '',
          timestamp: DateTime.now(),
        );
        break;
      case AguiEventType.toolCallResult:
        entry = _OutputEntry(
          type: _EntryType.toolResult,
          title: 'Result',
          content: event.content?.toString() ?? '',
          timestamp: DateTime.now(),
        );
        break;
      case AguiEventType.runError:
        entry = _OutputEntry(
          type: _EntryType.error,
          title: 'Error',
          content: event.message ?? 'Unknown error',
          timestamp: DateTime.now(),
        );
        break;
      case AguiEventType.reasoningMessageContent:
        final delta = event.delta;
        if (delta != null && delta.isNotEmpty) {
          // Append to existing reasoning entry or create new
          if (_entries.isNotEmpty &&
              _entries.last.type == _EntryType.reasoning) {
            setState(() {
              _entries.last = _entries.last.copyWith(
                content: _entries.last.content + delta,
              );
            });
            _scrollToBottom();
            return;
          }
          entry = _OutputEntry(
            type: _EntryType.reasoning,
            title: 'Thinking',
            content: delta,
            timestamp: DateTime.now(),
          );
        }
        break;
      case AguiEventType.stepStarted:
        entry = _OutputEntry(
          type: _EntryType.step,
          title: 'turn',
          content: 'Started',
          timestamp: DateTime.now(),
        );
        break;
      case AguiEventType.custom:
        if (event.customName == 'query_prompt') {
          final value = event.customValue;
          final text = value is Map ? (value['text'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.step,
            title: 'query',
            content: text.toString(),
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'container_restart') {
          final value = event.customValue;
          final reason = value is Map ? (value['reason'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.step,
            title: 'Container Restart',
            content: reason.toString(),
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'container_starting') {
          final value = event.customValue;
          final reason = value is Map ? (value['reason'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.step,
            title: 'Container Starting',
            content: reason.toString(),
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'container_ready') {
          final value = event.customValue;
          final reason = value is Map ? (value['reason'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.step,
            title: 'Container Ready',
            content: reason.toString(),
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'session_resume') {
          final value = event.customValue;
          final reason = value is Map ? (value['reason'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.step,
            title: 'Session Resume',
            content: reason.toString(),
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'extension_ui_request') {
          final value = event.customValue;
          final method = value is Map ? (value['method'] ?? '') : '';
          final title = value is Map ? (value['title'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.toolCall,
            title: 'Extension UI: $method',
            content:
                title.length > 100 ? '${title.substring(0, 100)}...' : title,
            timestamp: DateTime.now(),
          );
        } else if (event.customName == 'container_stopped') {
          final value = event.customValue;
          final reason = value is Map ? (value['reason'] ?? '') : '';
          entry = _OutputEntry(
            type: _EntryType.error,
            title: 'Container Stopped',
            content: reason.toString(),
            timestamp: DateTime.now(),
          );
        }
        break;
    }

    if (entry != null) {
      setState(() => _entries.add(entry!));
      _scrollToBottom();
    }
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
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
            boxShadow: const [
              BoxShadow(
                  color: Color(0x30000000),
                  blurRadius: 2,
                  offset: Offset(0, 1)),
            ],
          ),
          child: Row(
            children: [
              const Icon(Icons.terminal, size: 16),
              const SizedBox(width: 4),
              const Text('Debug',
                  style: TextStyle(fontSize: 12, fontWeight: FontWeight.bold)),
              const Spacer(),
              IconButton(
                icon: const Icon(Icons.clear_all, size: 16),
                onPressed: () => setState(() => _entries.clear()),
                iconSize: 16,
                constraints: const BoxConstraints(),
                padding: EdgeInsets.zero,
              ),
            ],
          ),
        ),
        Expanded(
          child: _entries.isEmpty
              ? const Center(
                  child: Text('No output yet', style: TextStyle(fontSize: 12)))
              : SelectionArea(
                  child: ListView.builder(
                    controller: _scrollController,
                    padding: const EdgeInsets.all(4),
                    itemCount: _entries.length,
                    itemBuilder: (context, index) =>
                        _buildEntry(_entries[index]),
                  ),
                ),
        ),
      ],
    );
  }

  Widget _buildEntry(_OutputEntry entry) {
    final color = switch (entry.type) {
      _EntryType.toolCall => Colors.cyan,
      _EntryType.toolResult => Colors.green,
      _EntryType.error => Colors.red,
      _EntryType.reasoning => Colors.amber,
      _EntryType.step => Colors.blue,
    };

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 2),
      padding: const EdgeInsets.all(6),
      decoration: BoxDecoration(
        border: Border(left: BorderSide(color: color, width: 3)),
        color: color.withOpacity(0.05),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(
                entry.title,
                style: TextStyle(
                    fontSize: 11, fontWeight: FontWeight.bold, color: color),
              ),
              const Spacer(),
              Text(
                '${entry.timestamp.hour.toString().padLeft(2, '0')}:${entry.timestamp.minute.toString().padLeft(2, '0')}:${entry.timestamp.second.toString().padLeft(2, '0')}',
                style: const TextStyle(fontSize: 9, color: Colors.grey),
              ),
            ],
          ),
          if (entry.content.isNotEmpty)
            Text(
              entry.content.length > 500
                  ? '${entry.content.substring(0, 500)}...'
                  : entry.content,
              style: const TextStyle(fontSize: 11, fontFamily: 'monospace'),
            ),
        ],
      ),
    );
  }
}

enum _EntryType { toolCall, toolResult, error, reasoning, step }

class _OutputEntry {
  final _EntryType type;
  final String title;
  final String content;
  final DateTime timestamp;

  _OutputEntry({
    required this.type,
    required this.title,
    required this.content,
    required this.timestamp,
  });

  _OutputEntry copyWith({String? content}) {
    return _OutputEntry(
      type: type,
      title: title,
      content: content ?? this.content,
      timestamp: timestamp,
    );
  }
}
