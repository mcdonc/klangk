import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../../utils/web_helpers_web.dart';

/// Opens a tapped markdown link in a new browser tab. Top-level (not a closure)
/// so it matches [MarkdownTapLinkCallback] directly and stays unit-testable.
void handleMarkdownLink(String text, String? href, String? title) {
  if (href != null && href.isNotEmpty) {
    openUrl(href);
  }
}

/// Renders `.md` / `.markdown` files as rich CommonMark via
/// `flutter_markdown_plus`. Selectable, with tapped links opened in a new tab.
class MarkdownRenderer extends FileRenderer {
  @override
  String get id => 'markdown';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.article;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) =>
      file.extension == 'md' || file.extension == 'markdown';

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _MarkdownView(file: file);
}

class _MarkdownView extends StatefulWidget {
  const _MarkdownView({required this.file});

  final RenderableFile file;

  @override
  State<_MarkdownView> createState() => _MarkdownViewState();
}

class _MarkdownViewState extends State<_MarkdownView> {
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
        return Markdown(
          data: snapshot.data ?? '',
          selectable: true,
          onTapLink: handleMarkdownLink,
          padding: const EdgeInsets.all(12),
        );
      },
    );
  }
}
