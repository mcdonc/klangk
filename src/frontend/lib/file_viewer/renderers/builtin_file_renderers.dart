import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'raw_text_renderer.dart';

/// The built-in file renderers, in registration order. Later milestones append
/// richer renderers (markdown, image, code, …) ahead of the always-available
/// [RawTextRenderer] fallback.
List<FileRenderer> builtinFileRenderers() => [RawTextRenderer()];
