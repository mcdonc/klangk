import 'package:flutter/material.dart';

const _bar3d = BoxDecoration(
  gradient: LinearGradient(
    colors: [Color(0xFFD0D0D0), Color(0xFFE8E8E8), Color(0xFFD0D0D0)],
  ),
  boxShadow: [
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(-1, 0)),
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(1, 0)),
  ],
);

const _bar3dHorizontal = BoxDecoration(
  gradient: LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    colors: [Color(0xFFD0D0D0), Color(0xFFE8E8E8), Color(0xFFD0D0D0)],
  ),
  boxShadow: [
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(0, -1)),
    BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(0, 1)),
  ],
);

/// Split-pane IDE layout with resizable dividers and 3D edge bars.
class IdeLayout extends StatefulWidget {
  final Widget terminal;
  final Widget fileViewer;
  final Widget output;

  const IdeLayout({
    super.key,
    required this.terminal,
    required this.fileViewer,
    required this.output,
  });

  @override
  State<IdeLayout> createState() => _IdeLayoutState();
}

class _IdeLayoutState extends State<IdeLayout> {
  double _horizontalRatio = 0.5;
  double _verticalRatio = 0.7; // file viewer gets more space

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final totalWidth = constraints.maxWidth;
        final totalHeight = constraints.maxHeight;
        const bar = 6.0;

        // Account for: left bar + center divider + right bar = 3 bars horizontally
        final usableWidth = totalWidth - bar * 3;
        final leftWidth = usableWidth * _horizontalRatio;
        final rightWidth = usableWidth - leftWidth;

        // Account for: horizontal divider + bottom bar = 2 bars vertically
        final usableHeight = totalHeight - bar * 2;
        final topHeight = usableHeight * _verticalRatio;
        final bottomHeight = usableHeight - topHeight;

        return Row(
          children: [
            // Left edge bar
            Container(width: bar, decoration: _bar3d),
            // Chat panel (left)
            Container(
              width: leftWidth,
              height: totalHeight,
              color: const Color(0xFFF7F6F2), // cool bone (was chat)
              child: widget.terminal,
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
            // Right column
            SizedBox(
              width: rightWidth + bar, // include right edge bar
              child: Column(
                children: [
                  // File viewer
                  Container(
                    height: topHeight,
                    color: const Color(0xFFFFFEFC), // warm white (was files)
                    child: Row(
                      children: [
                        Expanded(child: widget.fileViewer),
                        Container(width: bar, decoration: _bar3d),
                      ],
                    ),
                  ),
                  // Horizontal divider
                  GestureDetector(
                    onVerticalDragUpdate: (details) {
                      setState(() {
                        _verticalRatio += details.delta.dy / usableHeight;
                        _verticalRatio = _verticalRatio.clamp(0.2, 0.8);
                      });
                    },
                    child: MouseRegion(
                      cursor: SystemMouseCursors.resizeRow,
                      child: Container(height: bar, decoration: _bar3dHorizontal),
                    ),
                  ),
                  // Output panel
                  Container(
                    height: bottomHeight,
                    color: const Color(0xFFF0EFE9),
                    child: Row(
                      children: [
                        Expanded(child: widget.output),
                        Container(width: bar, decoration: _bar3d),
                      ],
                    ),
                  ),
                  // Bottom bar
                  Container(height: bar, decoration: _bar3dHorizontal),
                ],
              ),
            ),
          ],
        );
      },
    );
  }
}
