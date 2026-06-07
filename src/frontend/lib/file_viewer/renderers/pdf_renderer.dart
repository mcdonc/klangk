import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:pdfrx/pdfrx.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../../theme/colors.dart';

/// Views `.pdf` files via `pdfrx` (pdfium; wasm on web). Reads bytes lazily and
/// offers simple prev/next page navigation.
class PdfRenderer extends FileRenderer {
  @override
  String get id => 'pdf';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.picture_as_pdf;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) => file.extension == 'pdf';

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _PdfView(file: file);
}

class _PdfView extends StatefulWidget {
  const _PdfView({required this.file});

  final RenderableFile file;

  @override
  State<_PdfView> createState() => _PdfViewState();
}

class _PdfViewState extends State<_PdfView> {
  final PdfViewerController _controller = PdfViewerController();
  late final Future<Uint8List> _bytes = widget.file.readBytes();
  int _page = 1;

  void _goToPage(int target) {
    if (target < 1) return;
    setState(() => _page = target);
    // The native controller only navigates once pdfium has loaded the document.
    // The Dart-VM test runner never loads pdfium, so this is guarded and the
    // navigation call is excluded from coverage (per project convention).
    if (_controller.isReady) {
      // coverage:ignore-start
      _controller.goToPage(pageNumber: target);
      // coverage:ignore-end
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Uint8List>(
      future: _bytes,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Padding(
            padding: const EdgeInsets.all(8),
            child: SelectableText('Failed to load PDF: ${snapshot.error}'),
          );
        }
        return Column(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              child: Row(
                children: [
                  IconButton(
                    icon: const Icon(Icons.chevron_left, size: 18),
                    tooltip: 'Previous page',
                    onPressed: () => _goToPage(_page - 1),
                  ),
                  Text('Page $_page', style: const TextStyle(fontSize: 12)),
                  IconButton(
                    icon: const Icon(Icons.chevron_right, size: 18),
                    tooltip: 'Next page',
                    onPressed: () => _goToPage(_page + 1),
                  ),
                  const Spacer(),
                  const Text(
                    'PDF',
                    style: TextStyle(
                      fontSize: 12,
                      color: KColors.textSecondary,
                    ),
                  ),
                ],
              ),
            ),
            Expanded(
              child: PdfViewer.data(
                snapshot.data!,
                sourceName: widget.file.path,
                controller: _controller,
              ),
            ),
          ],
        );
      },
    );
  }
}
