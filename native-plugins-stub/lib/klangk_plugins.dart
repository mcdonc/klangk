// Native-safe stand-in for the generated klangk_plugins aggregator.
// Returns no plugins so the desktop build can compile without the web-only
// beep/celebrate/soliplex packages. See pubspec.yaml for the rationale.
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

List<ToolPlugin> createAllPlugins() => <ToolPlugin>[];
