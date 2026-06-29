/// Self-contained terminal tab strip + active terminal rendering.
///
/// Extracted from `_WorkspacePageState._buildTerminalWithTabs` (#971).
import 'package:flutter/material.dart';
import '../theme/colors.dart';
import '../ws/ws_client.dart';
import '../terminal/ghostty_terminal.dart';
import '../utils/suppress_browser_menu.dart';

/// The tab bar + terminal body that was previously built inline by
/// `_WorkspacePageState._buildTerminalWithTabs`.
class TerminalTabsView extends StatelessWidget {
  final WsClient wsClient;
  final GlobalKey<GhosttyTerminalState> terminalKey;
  final void Function(
    ({String token, String? uri, String pwd, String tail}),
  ) onPathTap;

  /// Currently-selected own window ID (null = first window).
  final String? selectedOwnWindowId;

  /// The shared terminal we're viewing (null = own terminal).
  final Map<String, String>? activeSharedTerminal;

  /// Whether the user has a given permission.
  final bool Function(String perm) hasPerm;

  /// Switch to an isolated (own) terminal window.
  final void Function(WsClient wsClient, String windowId) onSwitchToIsolated;

  /// Join a shared terminal from another user.
  final void Function(WsClient wsClient, String userId, String windowId)
      onJoinShared;

  const TerminalTabsView({
    super.key,
    required this.wsClient,
    required this.terminalKey,
    required this.onPathTap,
    required this.selectedOwnWindowId,
    required this.activeSharedTerminal,
    required this.hasPerm,
    required this.onSwitchToIsolated,
    required this.onJoinShared,
  });

  bool _isWindowShared(String windowId) {
    final myUserId = wsClient.currentUserId;
    if (myUserId == null) return false;
    return wsClient.sharedTerminals.any(
      (s) => s['user_id'] == myUserId && s['window_id'] == windowId,
    );
  }

  List<Map<String, dynamic>> _getViewers(String ownerUserId, String windowId) {
    for (final s in wsClient.sharedTerminals) {
      if (s['user_id'] == ownerUserId && s['window_id'] == windowId) {
        final viewers = s['viewers'];
        if (viewers is List) {
          return viewers.cast<Map<String, dynamic>>();
        }
      }
    }
    return [];
  }

  @override
  Widget build(BuildContext context) {
    final windows = wsClient.terminalWindows;
    final shared = wsClient.sharedTerminals;
    final myUserId = wsClient.currentUserId;
    final othersShared = shared.where((s) => s['user_id'] != myUserId).toList();
    final hasContent = windows.isNotEmpty || othersShared.isNotEmpty;

    return Column(
      children: [
        if (hasContent)
          Container(
            height: 32,
            decoration: const BoxDecoration(
              color: KColors.bgAppBar,
              border: Border(bottom: BorderSide(color: KColors.borderMuted)),
            ),
            child: Row(
              children: [
                const SizedBox(width: 4),
                Expanded(
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    children: [
                      // Own terminal tabs with share/unshare toggle
                      if (hasPerm('code-in-isolation'))
                        for (final w in windows)
                          _TerminalTab(
                            name: w['name'] as String? ?? '?',
                            tooltip: w['name'] as String? ?? '?',
                            active: activeSharedTerminal == null &&
                                (selectedOwnWindowId != null
                                    ? (w['id'] as String?) ==
                                        selectedOwnWindowId
                                    : (w['active'] as bool? ?? false)),
                            isShared: _isWindowShared(
                              w['id'] as String? ?? '',
                            ),
                            viewers: _getViewers(
                              myUserId ?? '',
                              w['id'] as String? ?? '',
                            ),
                            onTap: () => onSwitchToIsolated(
                              wsClient,
                              w['id'] as String? ?? '',
                            ),
                            onClose: windows.length > 1
                                ? () => wsClient.sendTerminalCloseWindow(
                                      w['index'] as int,
                                    )
                                : null,
                            onRename: (newName) =>
                                wsClient.sendTerminalRenameWindow(
                              w['index'] as int,
                              newName,
                            ),
                            onToggleShare: hasPerm('share-terminals')
                                ? () {
                                    final wid = w['id'] as String? ?? '';
                                    if (_isWindowShared(wid)) {
                                      wsClient.sendUnshareWindow(wid);
                                    } else {
                                      wsClient.sendShareWindow(wid);
                                    }
                                  }
                                : null,
                          ),
                      // "+" for new terminal
                      if (hasPerm('code-in-isolation'))
                        _TabIconButton(
                          icon: Icons.add,
                          tooltip: 'New terminal',
                          onTap: () => wsClient.sendTerminalNewWindow(),
                        ),
                      // Shared terminals from OTHER users
                      for (final s in othersShared)
                        _TerminalTab(
                          name: () {
                            final h = s['handle'] as String? ?? '?';
                            final w = s['window_name'] as String? ?? '?';
                            final he =
                                h.length > 5 ? '${h.substring(0, 5)}…' : h;
                            final we =
                                w.length > 3 ? '${w.substring(0, 3)}…' : w;
                            return '$he:$we';
                          }(),
                          tooltip:
                              '${s['handle'] ?? '?'}:${s['window_name'] ?? '?'}',
                          active: activeSharedTerminal != null &&
                              activeSharedTerminal!['user_id'] ==
                                  s['user_id'] &&
                              activeSharedTerminal!['window_id'] ==
                                  s['window_id'],
                          shared: true,
                          readOnly: !hasPerm('code-in-shared-terminals') &&
                              !hasPerm('share-terminals'),
                          viewers: _getViewers(
                            s['user_id'] as String? ?? '',
                            s['window_id'] as String? ?? '',
                          ),
                          onTap: () => onJoinShared(
                            wsClient,
                            s['user_id'] as String,
                            s['window_id'] as String,
                          ),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        Expanded(
          child: GhosttyTerminal(
            key: terminalKey,
            wsClient: wsClient,
            onPathTap: onPathTap,
          ),
        ),
      ],
    );
  }
}

// ── Private tab widgets (moved from workspace_page.dart) ─────────────

class _TerminalTab extends StatefulWidget {
  final String name;
  final String? tooltip;
  final bool active;
  final bool shared;
  final bool readOnly;
  final bool isShared;
  final List<Map<String, dynamic>> viewers;
  final VoidCallback onTap;
  final VoidCallback? onClose;
  final VoidCallback? onToggleShare;
  final void Function(String newName)? onRename;

  const _TerminalTab({
    required this.name,
    required this.active,
    required this.onTap,
    this.tooltip,
    this.shared = false,
    this.readOnly = false,
    this.isShared = false,
    this.viewers = const [],
    this.onClose,
    this.onToggleShare,
    this.onRename,
  });

  @override
  State<_TerminalTab> createState() => _TerminalTabState();
}

class _TerminalTabState extends State<_TerminalTab> {
  bool _hovered = false;
  Offset? _tapPosition;

  void _showContextMenu() {
    final pos = _tapPosition;
    if (pos == null) return;
    final items = <PopupMenuEntry<String>>[
      if (widget.onRename != null)
        const PopupMenuItem(
          value: 'rename',
          child: ListTile(
            dense: true,
            leading: Icon(Icons.edit, size: 18),
            title: Text('Rename'),
          ),
        ),
      if (widget.onToggleShare != null)
        PopupMenuItem(
          value: 'share',
          child: ListTile(
            dense: true,
            leading: Icon(
              widget.isShared ? Icons.cell_tower : Icons.share_outlined,
              size: 18,
            ),
            title: Text(widget.isShared ? 'Unshare' : 'Share'),
          ),
        ),
    ];
    if (items.isEmpty) return;
    showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(pos.dx, pos.dy, pos.dx, pos.dy),
      items: items,
    ).then((action) {
      if (action == 'rename') {
        _showRenameDialog();
      } else if (action == 'share') {
        widget.onToggleShare?.call();
      }
    });
  }

  void _showRenameDialog() {
    final controller = TextEditingController(text: widget.name);
    showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Rename terminal'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'Terminal name'),
          onSubmitted: (v) => Navigator.of(ctx).pop(v.trim()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('OK'),
          ),
        ],
      ),
    ).then((newName) {
      if (newName != null && newName.isNotEmpty && newName != widget.name) {
        widget.onRename?.call(newName);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return _maybeTooltip(Padding(
      padding: const EdgeInsets.symmetric(horizontal: 1, vertical: 3),
      child: MouseRegion(
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        cursor: SystemMouseCursors.click,
        child: SuppressBrowserContextMenu(
          child: GestureDetector(
            onTap: widget.onTap,
            onSecondaryTapDown: (details) {
              _tapPosition = details.globalPosition;
            },
            onSecondaryTap: _showContextMenu,
            child: SizedBox(
              width: 120,
              child: Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
                decoration: BoxDecoration(
                  color: widget.active
                      ? KColors.bgSurface
                      : _hovered
                          ? KColors.bgOverlay
                          : Colors.transparent,
                  borderRadius: BorderRadius.circular(4),
                  border: widget.active
                      ? Border.all(color: KColors.borderMuted, width: 0.5)
                      : null,
                ),
                child: Row(
                  children: [
                    // Icon for other users' shared tabs (left of name)
                    if (widget.shared) ...[
                      Icon(
                        widget.readOnly
                            ? Icons.lock_outlined
                            : Icons.edit_outlined,
                        size: 12,
                        color: widget.active
                            ? KColors.accentAmber
                            : Colors.white38,
                      ),
                      const SizedBox(width: 4),
                    ],
                    // Broadcast icon for own tabs that are actively shared
                    // — click to unshare
                    if (!widget.shared && widget.isShared) ...[
                      GestureDetector(
                        onTap: widget.onToggleShare,
                        child: const MouseRegion(
                          cursor: SystemMouseCursors.click,
                          child: Tooltip(
                            message: 'Unshare',
                            child: Icon(
                              Icons.cell_tower,
                              size: 12,
                              color: KColors.accentCyan,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 4),
                    ],
                    Expanded(
                      child: Text(
                        widget.name,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: widget.active
                              ? FontWeight.w600
                              : FontWeight.normal,
                          color: widget.active
                              ? KColors.textPrimary
                              : _hovered
                                  ? Colors.white70
                                  : KColors.textSecondary,
                        ),
                      ),
                    ),
                    // Viewer count
                    if (widget.viewers.isNotEmpty) ...[
                      const SizedBox(width: 4),
                      Icon(
                        Icons.visibility,
                        size: 10,
                        color: Colors.white38,
                      ),
                      const SizedBox(width: 2),
                      Text(
                        '${widget.viewers.length}',
                        style: const TextStyle(
                          fontSize: 10,
                          color: Colors.white38,
                        ),
                      ),
                    ],
                    if (widget.onClose != null) ...[
                      const SizedBox(width: 4),
                      MouseRegion(
                        cursor: SystemMouseCursors.click,
                        child: GestureDetector(
                          onTap: widget.onClose,
                          child: Icon(
                            Icons.close,
                            size: 12,
                            color: _hovered
                                ? Colors.white70
                                : widget.active
                                    ? Colors.white38
                                    : Colors.transparent,
                          ),
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    ));
  }

  Widget _maybeTooltip(Widget child) {
    final parts = <String>[];
    if (widget.tooltip != null) {
      parts.add(widget.tooltip!);
    }
    if (widget.viewers.isNotEmpty) {
      final names = widget.viewers
          .map((v) => (v['email'] as String?)?.split('@').first ?? '?')
          .join(', ');
      parts.add('👁 $names');
    }
    if (parts.isNotEmpty) {
      return Tooltip(message: parts.join('\n'), child: child);
    }
    return child;
  }
}

class _TabIconButton extends StatefulWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  const _TabIconButton({
    required this.icon,
    required this.tooltip,
    required this.onTap,
  });

  @override
  State<_TabIconButton> createState() => _TabIconButtonState();
}

class _TabIconButtonState extends State<_TabIconButton> {
  bool _hovered = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      child: MouseRegion(
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        cursor: SystemMouseCursors.click,
        child: GestureDetector(
          onTap: widget.onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
            decoration: BoxDecoration(
              color: _hovered ? KColors.bgOverlay : Colors.transparent,
              borderRadius: BorderRadius.circular(4),
            ),
            child: Icon(
              widget.icon,
              size: 14,
              color: _hovered ? Colors.white : Colors.white54,
            ),
          ),
        ),
      ),
    );
  }
}
