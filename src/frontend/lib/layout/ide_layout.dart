import 'package:flutter/material.dart';
import '../terminal/container_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';

/// IDE layout: skeuomorphic tabs (Terminal + Files) taking the full width.
class IdeLayout extends StatefulWidget {
  final Widget fileViewer;
  final Widget terminal;
  final GlobalKey<ContainerTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;

  const IdeLayout({
    super.key,
    required this.fileViewer,
    required this.terminal,
    this.terminalKey,
    this.fileViewerKey,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout> {
  int _selectedIndex = 0;

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
    return Column(
      children: [
        // Tab bar
        Container(
          height: 36,
          color: const Color(0xFFD0CFC8),
          child: Row(
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
        // Content
        Expanded(
          child: IndexedStack(
            index: _selectedIndex,
            children: [
              Container(
                color: const Color(0xFF1D1F21),
                padding: const EdgeInsets.only(left: 5),
                child: widget.terminal,
              ),
              Container(
                color: const Color(0xFFFFFEFC),
                child: widget.fileViewer,
              ),
            ],
          ),
        ),
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
      child: Container(
        margin: const EdgeInsets.only(left: 2, top: 4),
        padding: const EdgeInsets.symmetric(horizontal: 16),
        decoration: BoxDecoration(
          color: isSelected ? const Color(0xFFF7F6F2) : const Color(0xFFBEBDB6),
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(8),
            topRight: Radius.circular(8),
          ),
          border: Border(
            top: BorderSide(
              color: isSelected
                  ? const Color(0xFF8A8880)
                  : const Color(0xFFA0A098),
              width: 1,
            ),
            left: BorderSide(
              color: isSelected
                  ? const Color(0xFF8A8880)
                  : const Color(0xFFA0A098),
              width: 1,
            ),
            right: BorderSide(
              color: isSelected
                  ? const Color(0xFF8A8880)
                  : const Color(0xFFA0A098),
              width: 1,
            ),
          ),
          boxShadow: isSelected
              ? const [
                  BoxShadow(
                    color: Color(0x20000000),
                    blurRadius: 3,
                    offset: Offset(0, -1),
                  ),
                ]
              : null,
        ),
        child: Row(
          children: [
            Icon(
              icon,
              size: 14,
              color: isSelected
                  ? const Color(0xFF3C3C3C)
                  : const Color(0xFF707068),
            ),
            const SizedBox(width: 6),
            Text(
              label,
              style: TextStyle(
                fontSize: 12,
                fontWeight: isSelected ? FontWeight.w600 : FontWeight.normal,
                color: isSelected
                    ? const Color(0xFF3C3C3C)
                    : const Color(0xFF707068),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
