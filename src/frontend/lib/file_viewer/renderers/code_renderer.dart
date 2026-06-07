import 'package:flutter/material.dart';
import 'package:flutter_highlight/flutter_highlight.dart';
import 'package:flutter_highlight/themes/atom-one-dark.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Maps a (lowercased, no-dot) file extension to a `highlight` language id.
/// Extensions not present here are not handled by [CodeRenderer.canRender];
/// [languageForExtension] still defends with a `plaintext` fallback.
const _languageByExtension = {
  'dart': 'dart',
  'py': 'python',
  'ts': 'typescript',
  'tsx': 'typescript',
  'js': 'javascript',
  'jsx': 'javascript',
  'mjs': 'javascript',
  'json': 'json',
  'yaml': 'yaml',
  'yml': 'yaml',
  'sh': 'bash',
  'bash': 'bash',
  'zsh': 'bash',
  'go': 'go',
  'rs': 'rust',
  'c': 'cpp',
  'h': 'cpp',
  'cpp': 'cpp',
  'cc': 'cpp',
  'cxx': 'cpp',
  'hpp': 'cpp',
  'css': 'css',
  'scss': 'scss',
  'html': 'xml',
  'htm': 'xml',
  'xml': 'xml',
};

/// The `highlight` language id for [extension], or `plaintext` when unknown.
String languageForExtension(String extension) =>
    _languageByExtension[extension] ?? 'plaintext';

/// Read-only, syntax-highlighted view for code files via `flutter_highlight`.
class CodeRenderer extends FileRenderer {
  @override
  String get id => 'code';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.code;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) =>
      _languageByExtension.containsKey(file.extension);

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _CodeView(file: file);
}

class _CodeView extends StatefulWidget {
  const _CodeView({required this.file});

  final RenderableFile file;

  @override
  State<_CodeView> createState() => _CodeViewState();
}

class _CodeViewState extends State<_CodeView> {
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
          scrollDirection: Axis.vertical,
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: HighlightView(
              snapshot.data ?? '',
              language: languageForExtension(widget.file.extension),
              theme: atomOneDarkTheme,
              padding: const EdgeInsets.all(12),
              textStyle: TextStyle(
                fontFamily: GoogleFonts.robotoMono().fontFamily,
                fontSize: 13,
              ),
            ),
          ),
        );
      },
    );
  }
}
