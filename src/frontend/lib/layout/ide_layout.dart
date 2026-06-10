import 'package:flutter/material.dart';
import '../terminal/ghostty_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../chat/workspace_chat.dart';
import '../theme/colors.dart';
import '../widgets/skeuo_tab.dart';

/// IDE layout: tabs (Terminal + Files + Chat) with optional
/// debug pane at the bottom separated by a draggable divider.
class IdeLayout extends StatefulWidget {
  final Widget fileViewer;
  final Widget terminal;
  final Widget? chat;
  final Widget? settings;
  final Widget? sharing;
  final Widget? debug;
  final int chatUnread;
  final bool chatMentioned;
  final GlobalKey<GhosttyTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;
  final GlobalKey<WorkspaceChatState>? chatKey;

  /// Deep-linked workspace-relative file to open in the Files tab on load (and
  /// whenever it changes). Null/empty (with no [initialDir]) shows Terminal.
  final String? initialFile;

  /// Deep-linked workspace-relative directory to browse in the Files tab on
  /// load. Used when [initialFile] is null/empty.
  final String? initialDir;

  const IdeLayout({
    super.key,
    required this.fileViewer,
    required this.terminal,
    this.chat,
    this.settings,
    this.sharing,
    this.debug,
    this.chatUnread = 0,
    this.chatMentioned = false,
    this.terminalKey,
    this.fileViewerKey,
    this.chatKey,
    this.initialFile,
    this.initialDir,
  });

  @override
  State<IdeLayout> createState() => IdeLayoutState();
}

class IdeLayoutState extends State<IdeLayout> {
  int _selectedIndex = 0;
  double _debugHeight = 0; // collapsed by default

  static const _dividerHeight = 6.0;
  static const _minDebugHeight = 0.0;
  static const _maxDebugHeight = 500.0;

  @override
  void initState() {
    super.initState();
    // Focus the pane shown first (Terminal by default) so the user can type
    // immediately on workspace open, without an extra click into it.
    _focusPane(_selectedIndex);
    _maybeOpenInitial();
  }

  @override
  void didUpdateWidget(IdeLayout oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.initialFile != oldWidget.initialFile ||
        widget.initialDir != oldWidget.initialDir) {
      _maybeOpenInitial();
    }
  }

  /// Opens the deep-linked [IdeLayout.initialFile] (preferred) or
  /// [IdeLayout.initialDir] in the Files tab once the panel is built. Deferred
  /// to after the frame so the fileViewer's state is attached.
  void _maybeOpenInitial() {
    final file = widget.initialFile;
    final dir = widget.initialDir;
    final hasFile = file != null && file.isNotEmpty;
    final hasDir = dir != null && dir.isNotEmpty;
    if (!hasFile && !hasDir) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (hasFile) {
        openFile(file);
      } else {
        openDirectory(dir!);
      }
    });
  }

  /// Switches to the Files tab and opens [path] in the existing viewer.
  void openFile(String path) {
    _selectTab(1);
    widget.fileViewerKey?.currentState?.openFile(path);
  }

  /// Switches to the Files tab and browses directory [path].
  void openDirectory(String path) {
    _selectTab(1);
    widget.fileViewerKey?.currentState?.openDir(path);
  }

  void _selectTab(int index) {
    final changed = index != _selectedIndex;
    if (changed) {
      setState(() => _selectedIndex = index);
      if (index == 1) {
        widget.fileViewerKey?.currentState?.refresh();
      }
      // Notify chat widget of visibility change.
      final chatIdx = widget.chat != null ? 2 : -1;
      widget.chatKey?.currentState?.setVisible(index == chatIdx);
    }
    // Always (re)focus the tab's input — even when re-clicking the already
    // active tab — so clicking Terminal/Chat returns focus to its input.
    _focusPane(index);
  }

  /// Focuses the input of the pane at [index] (Terminal or Chat). Deferred to
  /// after the frame so the target's FocusNode is attached and the pane is
  /// visible in the IndexedStack before we request focus.
  void _focusPane(int index) {
    final chatIdx = widget.chat != null ? 2 : -1;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (index == 0) {
        widget.terminalKey?.currentState?.requestFocus();
      } else if (index == chatIdx) {
        widget.chatKey?.currentState?.requestFocus();
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final hasDebug = widget.debug != null;
    final hasChat = widget.chat != null;
    final hasSettings = widget.settings != null;

    // Build dynamic tab list: Terminal(0), Files(1), Chat?(2), Settings?(last)
    final tabs = <Widget>[
      SkeuoTab(
        label: 'Terminal',
        icon: Icons.terminal,
        isSelected: _selectedIndex == 0,
        onTap: () => _selectTab(0),
      ),
      SkeuoTab(
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
      tabs.add(SkeuoTab(
        label: 'Chat',
        icon: Icons.chat_outlined,
        isSelected: _selectedIndex == chatIndex,
        badge: widget.chatUnread > 0 ? widget.chatUnread : null,
        badgeHighlight: widget.chatMentioned,
        onTap: () => _selectTab(chatIndex),
      ));
      content.add(Container(
        color: KColors.bgCanvas,
        child: widget.chat!,
      ));
    }
    if (widget.sharing != null) {
      final sharingIndex = tabs.length;
      tabs.add(SkeuoTab(
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
    if (hasSettings) {
      final settingsIndex = tabs.length;
      tabs.add(SkeuoTab(
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
