import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Always-available fallback renderer: shows the file's decoded text in a
/// monospace [SelectableText]. This preserves the workspace's original
/// file-viewing behavior and is offered for every file (lowest priority, so a
/// richer renderer wins the default slot when one matches).
class RawTextRenderer extends FileRenderer {
  @override
  String get id => 'raw';

  @override
  String get modeLabel => 'Raw';

  @override
  IconData get icon => Icons.subject;

  // Lowest-but-nonzero: always offered, but any typed renderer (priority >= 10)
  // wins the default slot.
  @override
  int get priority => 1;

  @override
  bool canRender(RenderableFile file) => true;

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _RawTextView(file: file);
}

class _RawTextView extends StatefulWidget {
  const _RawTextView({required this.file});

  final RenderableFile file;

  @override
  State<_RawTextView> createState() => _RawTextViewState();
}

class _RawTextViewState extends State<_RawTextView> {
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
        return SingleChildScrollView(
          padding: const EdgeInsets.all(8),
          child: SizedBox(
            width: double.infinity,
            child: SelectableText(
              snapshot.data ?? '',
              style: TextStyle(
                fontFamily: GoogleFonts.robotoMono().fontFamily,
                fontSize: 14,
              ),
              textAlign: TextAlign.left,
            ),
          ),
        );
      },
    );
  }
}
