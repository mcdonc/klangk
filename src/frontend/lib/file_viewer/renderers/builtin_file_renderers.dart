import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'markdown_renderer.dart';
import 'raw_text_renderer.dart';

/// The built-in file renderers, in registration order. Richer renderers come
/// first (higher priority wins the default slot); the always-available
/// [RawTextRenderer] fallback is last.
List<FileRenderer> builtinFileRenderers() => [
      MarkdownRenderer(),
      RawTextRenderer(),
    ];
