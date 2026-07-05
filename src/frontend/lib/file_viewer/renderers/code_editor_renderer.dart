import 'package:code_forge_web/code_forge_web.dart';
import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../../theme/colors.dart';
import 'code_renderer.dart' show isCodeExtension;

/// Editable code view via `code_forge_web` (the web-compatible CodeForge
/// variant). Offered for the same extensions as the read-only code view, at a
/// lower priority so View stays the default. Saving uses
/// [RenderableFile.saveText] when available; otherwise the buffer is local-only.
class CodeEditorRenderer extends FileRenderer {
  @override
  String get id => 'edit';

  @override
  String get modeLabel => 'Edit';

  @override
  IconData get icon => Icons.edit;

  // Below the read-only code View (10) but above Raw (1), so View is the
  // default and Edit is offered as a second mode.
  @override
  int get priority => 5;

  @override
  bool canRender(RenderableFile file) => isCodeExtension(file.extension);

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _CodeEditorView(file: file);
}

class _CodeEditorView extends StatefulWidget {
  const _CodeEditorView({required this.file});

  final RenderableFile file;

  @override
  State<_CodeEditorView> createState() => _CodeEditorViewState();
}

class _CodeEditorViewState extends State<_CodeEditorView> {
  final CodeForgeWebController _controller = CodeForgeWebController();
  late final Future<void> _loaded = _load();
  bool _saving = false;
  String? _status;

  Future<void> _load() async {
    _controller.text = await widget.file.readText();
  }

  Future<void> _save() async {
    final save = widget.file.saveText!;
    setState(() {
      _saving = true;
      _status = null;
    });
    try {
      await save(_controller.text);
      if (mounted) setState(() => _status = 'Saved');
    } catch (e) {
      debugPrint('Save failed: $e');
      if (mounted) setState(() => _status = 'Save failed. Please try again.');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<void>(
      future: _loaded,
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
        final canSave = widget.file.saveText != null;
        return Column(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              child: Row(
                children: [
                  if (_status != null)
                    Text(_status!, style: const TextStyle(fontSize: 12)),
                  const Spacer(),
                  if (canSave)
                    TextButton.icon(
                      icon: const Icon(Icons.save, size: 16),
                      label: const Text('Save'),
                      onPressed: _saving ? null : _save,
                    )
                  else
                    const Text(
                      'read-only',
                      style: TextStyle(
                        fontSize: 12,
                        color: KColors.textSecondary,
                      ),
                    ),
                ],
              ),
            ),
            Expanded(
              child: CodeForgeWeb(controller: _controller),
            ),
          ],
        );
      },
    );
  }
}
