import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'renderers/builtin_file_renderers.dart';

/// Builds the file-renderer registry for the workspace: the built-in renderers
/// first, then renderers contributed by any plugin that also implements
/// [FileRendererPlugin] (a plugin may implement `ToolPlugin`, that, or both).
FileRendererRegistry buildFileRendererRegistry(Iterable<ToolPlugin> plugins) {
  final registry = FileRendererRegistry()..registerAll(builtinFileRenderers());
  for (final plugin in plugins) {
    if (plugin is FileRendererPlugin) {
      registry.registerAll((plugin as FileRendererPlugin).fileRenderers);
    }
  }
  return registry;
}
