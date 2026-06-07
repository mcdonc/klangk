import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'code_editor_renderer.dart';
import 'code_renderer.dart';
import 'image_renderer.dart';
import 'markdown_renderer.dart';
import 'pdf_renderer.dart';
import 'raw_text_renderer.dart';

/// The built-in file renderers, in registration order. Richer renderers come
/// first (higher priority wins the default slot); the always-available
/// [RawTextRenderer] fallback is last.
List<FileRenderer> builtinFileRenderers() => [
      MarkdownRenderer(),
      ImageRenderer(),
      CodeRenderer(),
      CodeEditorRenderer(),
      PdfRenderer(),
      RawTextRenderer(),
    ];
