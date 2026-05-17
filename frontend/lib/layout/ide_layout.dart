import 'package:flutter/material.dart';
import '../terminal/container_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';

const _bar3d = BoxDecoration(
  gradient: LinearGradient(
    colors: [Color(0xFFB8B8B8), Color(0xFFE8E8E8), Color(0xFFB8B8B8)],
  ),
);

const _bar3dHorizontal = BoxDecoration(
  gradient: LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    colors: [Color(0xFFB8B8B8), Color(0xFFE8E8E8), Color(0xFFB8B8B8)],
  ),
);

/// Split-pane IDE layout: chat on left, tabs + debug on right.
class IdeLayout extends StatefulWidget {
  final Widget chat;
  final Widget fileViewer;
  final Widget terminal;
  final GlobalKey<ContainerTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;
  final Widget output;

  const IdeLayout({
    super.key,
    required this.chat,
    required this.fileViewer,
    required this.terminal,
    this.terminalKey,
    this.fileViewerKey,
    required this.output,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout>
    with SingleTickerProviderStateMixin {
  double _horizontalRatio = 0.38;
  double _verticalRatio = 1.0; // debug collapsed by default
  late final TabController _tabController;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _tabController.addListener(() {
      if (_tabController.index == 0) {
        widget.terminalKey?.currentState?.requestFocus();
      } else if (_tabController.index == 1) {
        widget.fileViewerKey?.currentState?.refresh();
      }
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final totalWidth = constraints.maxWidth;
        final totalHeight = constraints.maxHeight;
        const bar = 6.0;
        const minDebug = 28.0;

        final leftWidth = totalWidth * _horizontalRatio;
        final dividerLeft = leftWidth;
        final rightWidth = totalWidth - leftWidth - bar;

        // Vertical split: tabs area + horizontal divider + debug pane
        final rightHeight = totalHeight;
        final tabsHeight = (rightHeight - bar - minDebug) * _verticalRatio;
        final debugTop = tabsHeight + bar;

        return Stack(
          children: [
            // Chat panel (left)
            Positioned(
              left: 0,
              top: 0,
              width: leftWidth,
              height: totalHeight,
              child: Container(
                color: const Color(0xFFF7F6F2),
                child: widget.chat,
              ),
            ),
            // Right column: tabs area (Files + Terminal)
            Positioned(
              left: leftWidth + bar,
              top: 0,
              right: 0,
              height: tabsHeight,
              child: Column(
                children: [
                  Container(
                    height: 32,
                    decoration: BoxDecoration(
                      color:
                          Theme.of(context).colorScheme.surfaceContainerHighest,
                      boxShadow: const [
                        BoxShadow(
                            color: Color(0x30000000),
                            blurRadius: 2,
                            offset: Offset(0, 1)),
                      ],
                    ),
                    child: TabBar(
                      controller: _tabController,
                      labelStyle: const TextStyle(
                          fontSize: 12, fontWeight: FontWeight.bold),
                      unselectedLabelStyle: const TextStyle(fontSize: 12),
                      indicatorSize: TabBarIndicatorSize.tab,
                      tabs: const [
                        Tab(text: 'Terminal'),
                        Tab(text: 'Files'),
                      ],
                    ),
                  ),
                  Expanded(
                    child: ListenableBuilder(
                      listenable: _tabController,
                      builder: (context, _) => IndexedStack(
                        index: _tabController.index,
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
                  ),
                ],
              ),
            ),
            // Horizontal divider between tabs and debug
            Positioned(
              left: leftWidth + bar,
              top: tabsHeight,
              right: 0,
              height: bar,
              child: GestureDetector(
                onVerticalDragUpdate: (details) {
                  setState(() {
                    _verticalRatio +=
                        details.delta.dy / (rightHeight - bar - minDebug);
                    _verticalRatio = _verticalRatio.clamp(0.2, 1.0);
                  });
                },
                child: MouseRegion(
                  cursor: SystemMouseCursors.resizeRow,
                  child: Container(decoration: _bar3dHorizontal),
                ),
              ),
            ),
            // Debug pane (below tabs)
            Positioned(
              left: leftWidth + bar,
              top: debugTop,
              right: 0,
              bottom: 0,
              child: Container(
                color: const Color(0xFFF0EFE9),
                child: widget.output,
              ),
            ),
            // Center vertical divider (on top so shadow renders over both panels)
            Positioned(
              left: dividerLeft,
              top: 0,
              width: bar,
              height: totalHeight,
              child: GestureDetector(
                onHorizontalDragUpdate: (details) {
                  setState(() {
                    _horizontalRatio += details.delta.dx / totalWidth;
                    _horizontalRatio = _horizontalRatio.clamp(0.2, 0.8);
                  });
                },
                child: MouseRegion(
                  cursor: SystemMouseCursors.resizeColumn,
                  child: Container(decoration: _bar3d),
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}
