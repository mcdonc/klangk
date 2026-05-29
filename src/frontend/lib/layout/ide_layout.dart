import 'package:flutter/material.dart';
import '../terminal/container_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../theme/colors.dart';

/// IDE layout: tabs (Terminal + Files) with optional
/// debug pane at the bottom separated by a draggable divider.
class IdeLayout extends StatefulWidget {
  final Widget fileViewer;
  final Widget terminal;
  final Widget? debug;
  final GlobalKey<ContainerTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;

  const IdeLayout({
    super.key,
    required this.fileViewer,
    required this.terminal,
    this.debug,
    this.terminalKey,
    this.fileViewerKey,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout> {
  int _selectedIndex = 0;
  double _debugHeight = 0; // collapsed by default

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
  }

  @override
  Widget build(BuildContext context) {
    final hasDebug = widget.debug != null;

    return Column(
      children: [
        // Tab bar
        Container(
          height: 40,
          color: KColors.bgCanvas,
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Expanded(
                child: _SkeuoTab(
                  label: 'Terminal',
                  icon: Icons.terminal,
                  isSelected: _selectedIndex == 0,
                  onTap: () => _selectTab(0),
                ),
              ),
              Expanded(
                child: _SkeuoTab(
                  label: 'Files',
                  icon: Icons.folder_outlined,
                  isSelected: _selectedIndex == 1,
                  onTap: () => _selectTab(1),
                ),
              ),
            ],
          ),
        ),
        // Content area
        Expanded(
          child: ClipRect(
            child: IndexedStack(
              index: _selectedIndex,
              children: [
                Container(
                  color: KColors.bgCanvas,
                  padding: const EdgeInsets.only(left: 5),
                  child: widget.terminal,
                ),
                Container(
                  color: KColors.bgOverlay,
                  child: widget.fileViewer,
                ),
              ],
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
  final VoidCallback onTap;

  const _SkeuoTab({
    required this.label,
    required this.icon,
    required this.isSelected,
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
          color: isSelected ? KColors.bgSurface : KColors.bgCanvas,
          child: Row(
            children: [
              Icon(
                icon,
                size: 14,
                color: isSelected ? KColors.textPrimary : KColors.textSecondary,
              ),
              const SizedBox(width: 6),
              Text(
                label,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: isSelected ? FontWeight.w600 : FontWeight.normal,
                  color:
                      isSelected ? KColors.textPrimary : KColors.textSecondary,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
