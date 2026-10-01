import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../html_preview_stub.dart'
    if (dart.library.js_interop) '../html_preview_web.dart';
import 'code_renderer.dart';

/// Displays `.html` / `.htm` files as a rendered page.
///
/// On the web the page shows in a sandboxed iframe: markup and inline CSS
/// render, while scripts, form submission and navigation stay off. Native
/// builds show the highlighted source through [CodeRenderer].
class HtmlRenderer extends FileRenderer {
  HtmlRenderer({this.previewSupported = htmlPreviewSupported});

  /// Whether this platform can display the page; defaults per platform.
  final bool previewSupported;

  @override
  String get id => 'html';

  @override
  String get modeLabel => 'Preview';

  @override
  IconData get icon => Icons.web;

  /// Above [CodeRenderer]'s 10, so a rendered page is the default view.
  @override
  int get priority => 20;

  @override
  bool canRender(RenderableFile file) =>
      file.extension == 'html' || file.extension == 'htm';

  @override
  Widget build(BuildContext context, RenderableFile file) => previewSupported
      ? _HtmlView(file: file)
      : CodeRenderer().build(context, file);
}

class _HtmlView extends StatefulWidget {
  const _HtmlView({required this.file});

  final RenderableFile file;

  @override
  State<_HtmlView> createState() => _HtmlViewState();
}

class _HtmlViewState extends State<_HtmlView> {
  late final Future<String> _content = widget.file.readText();

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<String>(
      future: _content,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Padding(
            padding: const EdgeInsets.all(8),
            child: SelectableText('Failed to load file: ${snapshot.error}'),
          );
        }
        return buildHtmlPreview(snapshot.data ?? '');
      },
    );
  }
}
