import 'package:flutter/material.dart';
import '../terminal/container_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';

/// IDE layout: tabs (Terminal + Files) taking the full width.
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

class _IdeLayoutState extends State<IdeLayout>
    with SingleTickerProviderStateMixin {
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
    return Column(
      children: [
        Container(
          height: 32,
          decoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
            boxShadow: const [
              BoxShadow(
                  color: Color(0x30000000),
                  blurRadius: 2,
                  offset: Offset(0, 1)),
            ],
          ),
          child: TabBar(
            controller: _tabController,
            labelStyle:
                const TextStyle(fontSize: 12, fontWeight: FontWeight.bold),
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
    );
  }
}
