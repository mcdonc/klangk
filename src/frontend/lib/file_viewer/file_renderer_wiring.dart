import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'renderers/builtin_file_renderers.dart';

/// Builds the file-renderer registry for the workspace: the built-in renderers
/// first, then renderers contributed by any feature that also implements
/// [FileRendererPlugin] (a feature may implement `ToolPlugin`, that, or both).
FileRendererRegistry buildFileRendererRegistry(Iterable<ToolPlugin> features) {
  final registry = FileRendererRegistry()..registerAll(builtinFileRenderers());
  for (final feature in features) {
    if (feature is FileRendererPlugin) {
      registry.registerAll((feature as FileRendererPlugin).fileRenderers);
    }
  }
  return registry;
}
