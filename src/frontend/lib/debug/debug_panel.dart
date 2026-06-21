import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../ws/ws_client.dart';
import 'system_info_tab.dart';

/// Debug panel with tabs: WebSocket message log and System Info.
/// Rendered inside IdeLayout's debug pane with a draggable divider above.
class DebugPanel extends StatefulWidget {
  final WsClient wsClient;

  const DebugPanel({super.key, required this.wsClient});

  @override
  State<DebugPanel> createState() => _DebugPanelState();
}

class _DebugPanelState extends State<DebugPanel> {
  int _tabIndex = 0;
  final List<WsDebugEntry> _entries = [];
  final ScrollController _scrollController = ScrollController();
  StreamSubscription<WsDebugEntry>? _sub;
  bool _autoScroll = true;
  static const _maxEntries = 500;

  @override
  void initState() {
    super.initState();
    _sub = widget.wsClient.debugLog.listen((entry) {
      setState(() {
        _entries.add(entry);
        if (_entries.length > _maxEntries) {
          _entries.removeRange(0, _entries.length - _maxEntries);
        }
      });
      if (_autoScroll) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (_scrollController.hasClients) {
            _scrollController
                .jumpTo(_scrollController.position.maxScrollExtent);
          }
        });
      }
    });
  }

  @override
  void dispose() {
    _sub?.cancel();
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF1A1A1A),
      child: Column(
        children: [
          // Tab bar
          Container(
            height: 22,
            color: const Color(0xFF2D2D2D),
            padding: const EdgeInsets.symmetric(horizontal: 8),
            child: Row(
              children: [
                _tabButton('WebSocket', 0),
                const SizedBox(width: 12),
                _tabButton('System', 1),
                const Spacer(),
                if (_tabIndex == 0) ...[
                  GestureDetector(
                    onTap: () => setState(() => _autoScroll = !_autoScroll),
                    child: Icon(
                      _autoScroll ? Icons.vertical_align_bottom : Icons.pause,
                      color: _autoScroll
                          ? const Color(0xFF5B8C5A)
                          : const Color(0xFF888888),
                      size: 12,
                    ),
                  ),
                  const SizedBox(width: 8),
                  GestureDetector(
                    onTap: () => setState(() => _entries.clear()),
                    child: const Icon(Icons.delete_outline,
                        color: Color(0xFF888888), size: 12),
                  ),
                ],
              ],
            ),
          ),
          // Content
          Expanded(
            child: IndexedStack(
              index: _tabIndex,
              children: [
                _buildWsTab(),
                SystemInfoTab(
                  auth: Provider.of<AuthService>(context, listen: false),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _tabButton(String label, int index) {
    final selected = _tabIndex == index;
    return GestureDetector(
      onTap: () => setState(() => _tabIndex = index),
      child: Text(
        label,
        style: TextStyle(
          color: selected ? const Color(0xFFC5C8C6) : const Color(0xFF888888),
          fontSize: 10,
          fontWeight: selected ? FontWeight.bold : FontWeight.normal,
        ),
      ),
    );
  }

  Widget _buildWsTab() {
    return ListView.builder(
      controller: _scrollController,
      itemCount: _entries.length,
      itemBuilder: (context, index) {
        final entry = _entries[index];
        return _DebugEntryRow(entry: entry);
      },
    );
  }
}

class _DebugEntryRow extends StatelessWidget {
  final WsDebugEntry entry;
  const _DebugEntryRow({required this.entry});

  @override
  Widget build(BuildContext context) {
    final isSend = entry.direction == 'SEND';
    final time =
        '${entry.timestamp.hour.toString().padLeft(2, '0')}:${entry.timestamp.minute.toString().padLeft(2, '0')}:${entry.timestamp.second.toString().padLeft(2, '0')}';
    final detail = entry.data != null
        ? const JsonEncoder.withIndent(null).convert(entry.data)
        : '';
    final displayDetail =
        detail.length > 200 ? '${detail.substring(0, 200)}...' : detail;

    return InkWell(
      onTap: entry.data != null ? () => _showFullMessage(context, entry) : null,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 55,
              child: Text(
                time,
                style: const TextStyle(
                  color: Color(0xFF666666),
                  fontSize: 10,
                  fontFamily: 'monospace',
                ),
              ),
            ),
            SizedBox(
              width: 36,
              child: Text(
                isSend ? 'SEND' : 'RECV',
                style: TextStyle(
                  color: isSend
                      ? const Color(0xFF81A2BE)
                      : const Color(0xFFB5BD68),
                  fontSize: 10,
                  fontWeight: FontWeight.bold,
                  fontFamily: 'monospace',
                ),
              ),
            ),
            Expanded(
              child: Text(
                '${entry.summary}  $displayDetail',
                style: const TextStyle(
                  color: Color(0xFFC5C8C6),
                  fontSize: 10,
                  fontFamily: 'monospace',
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }

  void _showFullMessage(BuildContext context, WsDebugEntry entry) {
    showDialog(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF1D1F21),
        title: Text(
          '${entry.direction} ${entry.summary}',
          style: const TextStyle(color: Color(0xFFC5C8C6), fontSize: 14),
        ),
        content: SingleChildScrollView(
          child: SelectableText(
            const JsonEncoder.withIndent('  ').convert(entry.data),
            style: const TextStyle(
              color: Color(0xFFC5C8C6),
              fontSize: 11,
              fontFamily: 'monospace',
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }
}
