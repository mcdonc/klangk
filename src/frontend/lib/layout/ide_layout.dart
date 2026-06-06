import 'package:flutter/material.dart';
import '../terminal/ghostty_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../chat/workspace_chat.dart';
import '../theme/colors.dart';

/// IDE layout: tabs (Terminal + Files + Chat) with optional
/// debug pane at the bottom separated by a draggable divider.
class IdeLayout extends StatefulWidget {
  final Widget fileViewer;
  final Widget terminal;
  final Widget? chat;
  final Widget? settings;
  final Widget? sharing;
  final Widget? debug;
  final GlobalKey<GhosttyTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;
  final GlobalKey<WorkspaceChatState>? chatKey;

  const IdeLayout({
    super.key,
    required this.fileViewer,
    required this.terminal,
    this.chat,
    this.settings,
    this.sharing,
    this.debug,
    this.terminalKey,
    this.fileViewerKey,
    this.chatKey,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout> {
  int _selectedIndex = 0;
  double _debugHeight = 0; // collapsed by default
  int _chatUnread = 0;

  static const _dividerHeight = 6.0;
  static const _minDebugHeight = 0.0;
  static const _maxDebugHeight = 500.0;

  void _selectTab(int index) {
    if (index == _selectedIndex) return;
    setState(() => _selectedIndex = index);
    if (index == 0) {
      widget.terminalKey?.currentState?.requestFocus();
    } else if (index == 1) {
      widget.fileViewerKey?.currentState?.refresh();
    }
    // Notify chat widget of visibility change
    final chatIdx = widget.chat != null ? 2 : -1;
    widget.chatKey?.currentState?.setVisible(index == chatIdx);
  }

  // coverage:ignore-start
  void _onChatUnreadChanged(int count) {
    if (mounted) setState(() => _chatUnread = count);
  }
  // coverage:ignore-end

  @override
  Widget build(BuildContext context) {
    final hasDebug = widget.debug != null;
    final hasChat = widget.chat != null;
    final hasSettings = widget.settings != null;

    // Build dynamic tab list: Terminal(0), Files(1), Chat?(2), Settings?(last)
    final tabs = <Widget>[
      _SkeuoTab(
        label: 'Terminal',
        icon: Icons.terminal,
        isSelected: _selectedIndex == 0,
        onTap: () => _selectTab(0),
      ),
      _SkeuoTab(
        label: 'Files',
        icon: Icons.folder_outlined,
        isSelected: _selectedIndex == 1,
        onTap: () => _selectTab(1),
      ),
    ];
    final content = <Widget>[
      Container(
        color: KColors.bgCanvas,
        padding: const EdgeInsets.only(left: 6, top: 4),
        child: widget.terminal,
      ),
      Container(
        color: KColors.bgCanvas,
        child: widget.fileViewer,
      ),
    ];
    if (hasChat) {
      final chatIndex = tabs.length;
      tabs.add(_SkeuoTab(
        label: 'Chat',
        icon: Icons.chat_outlined,
        isSelected: _selectedIndex == chatIndex,
        badge: _chatUnread > 0 ? _chatUnread : null,
        onTap: () => _selectTab(chatIndex),
      ));
      content.add(Container(
        color: KColors.bgCanvas,
        child: widget.chat!,
      ));
    }
    if (hasSettings) {
      final settingsIndex = tabs.length;
      tabs.add(_SkeuoTab(
        label: 'Settings',
        icon: Icons.settings,
        isSelected: _selectedIndex == settingsIndex,
        onTap: () => _selectTab(settingsIndex),
      ));
      content.add(Container(
        color: KColors.bgCanvas,
        child: widget.settings!,
      ));
    }
    if (widget.sharing != null) {
      final sharingIndex = tabs.length;
      tabs.add(_SkeuoTab(
        label: 'Sharing',
        icon: Icons.people_outline,
        isSelected: _selectedIndex == sharingIndex,
        onTap: () => _selectTab(sharingIndex),
      ));
      content.add(Container(
        color: KColors.bgCanvas,
        child: widget.sharing!,
      ));
    }

    return Column(
      children: [
        // Tab bar
        Container(
          height: 40,
          color: KColors.bgCanvas,
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: tabs.map((t) => Expanded(child: t)).toList(),
          ),
        ),
        // Content area
        Expanded(
          child: ClipRect(
            child: IndexedStack(
              index: _selectedIndex,
              children: content,
            ),
          ),
        ),
        // Debug divider + pane
        if (hasDebug) ...[
          GestureDetector(
            onVerticalDragUpdate: (details) {
              setState(() {
                _debugHeight = (_debugHeight - details.delta.dy)
                    .clamp(_minDebugHeight, _maxDebugHeight);
              });
            },
            onDoubleTap: () {
              setState(() {
                _debugHeight = _debugHeight > 0 ? 0 : 200;
              });
            },
            child: MouseRegion(
              cursor: SystemMouseCursors.resizeRow,
              child: Container(
                height: _dividerHeight,
                color: KColors.borderMuted,
                child: Center(
                  child: Container(
                    width: 40,
                    height: 3,
                    decoration: BoxDecoration(
                      color: KColors.textMuted,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
              ),
            ),
          ),
          SizedBox(
            height: _debugHeight,
            child: widget.debug!,
          ),
        ],
      ],
    );
  }
}

class _SkeuoTab extends StatelessWidget {
  final String label;
  final IconData icon;
  final bool isSelected;
  final int? badge;
  final VoidCallback onTap;

  const _SkeuoTab({
    required this.label,
    required this.icon,
    required this.isSelected,
    this.badge,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: ClipRRect(
        borderRadius: const BorderRadius.only(
          bottomLeft: Radius.circular(8),
          bottomRight: Radius.circular(8),
        ),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          color: isSelected ? KColors.bgCanvas : KColors.bgAppBar,
          child: Row(
            children: [
              Icon(
                icon,
                size: 14,
                color: KColors.textSecondary,
              ),
              const SizedBox(width: 6),
              Text(
                label,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: isSelected ? FontWeight.w700 : FontWeight.normal,
                  color: KColors.textSecondary,
                ),
              ),
              // coverage:ignore-start
              if (badge != null) ...[
                const SizedBox(width: 4),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(
                    color: KColors.accentRed,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    badge! > 99 ? '99+' : badge.toString(),
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 10,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
              // coverage:ignore-end
            ],
          ),
        ),
      ),
    );
  }
}
