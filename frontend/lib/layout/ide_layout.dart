import 'package:flutter/material.dart';
import '../terminal/container_terminal.dart';

const _bar3d = BoxDecoration(
  gradient: LinearGradient(
    colors: [Color(0xFFD0D0D0), Color(0xFFE8E8E8), Color(0xFFD0D0D0)],
  ),
  boxShadow: [
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(-1, 0)),
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(1, 0)),
  ],
);

/// Split-pane IDE layout: chat on left, tabbed panel on right.
class IdeLayout extends StatefulWidget {
  final Widget chat;
  final Widget fileViewer;
  final Widget terminal;
  final GlobalKey<ContainerTerminalState>? terminalKey;
  final Widget output;

  const IdeLayout({
    super.key,
    required this.chat,
    required this.fileViewer,
    required this.terminal,
    this.terminalKey,
    required this.output,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout>
    with SingleTickerProviderStateMixin {
  double _horizontalRatio = 0.38;
  late final TabController _tabController;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 3, vsync: this);
    _tabController.addListener(() {
      if (_tabController.index == 1) {
        widget.terminalKey?.currentState?.requestFocus();
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

        final usableWidth = totalWidth - bar * 3;
        final leftWidth = usableWidth * _horizontalRatio;
        final rightWidth = usableWidth - leftWidth;

        return Row(
          children: [
            // Left edge bar
            Container(width: bar, decoration: _bar3d),
            // Chat panel (left)
            Container(
              width: leftWidth,
              height: totalHeight,
              color: const Color(0xFFF7F6F2),
              child: widget.chat,
            ),
            // Center divider
            GestureDetector(
              onHorizontalDragUpdate: (details) {
                setState(() {
                  _horizontalRatio += details.delta.dx / usableWidth;
                  _horizontalRatio = _horizontalRatio.clamp(0.2, 0.8);
                });
              },
              child: MouseRegion(
                cursor: SystemMouseCursors.resizeColumn,
                child: Container(width: bar, decoration: _bar3d),
              ),
            ),
            // Right column: tabbed panel
            SizedBox(
              width: rightWidth + bar,
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      children: [
                        Container(
                          height: 32,
                          decoration: BoxDecoration(
                            color: Theme.of(context)
                                .colorScheme
                                .surfaceContainerHighest,
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
                            unselectedLabelStyle:
                                const TextStyle(fontSize: 12),
                            indicatorSize: TabBarIndicatorSize.tab,
                            tabs: const [
                              Tab(text: 'Files'),
                              Tab(text: 'Terminal'),
                              Tab(text: 'Debug'),
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
                                  color: const Color(0xFFFFFEFC),
                                  child: widget.fileViewer,
                                ),
                                Container(
                                  color: const Color(0xFFF0EFE9),
                                  child: widget.terminal,
                                ),
                                Container(
                                  color: const Color(0xFFF0EFE9),
                                  child: widget.output,
                                ),
                              ],
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                  Container(width: bar, decoration: _bar3d),
                ],
              ),
            ),
          ],
        );
      },
    );
  }
}
