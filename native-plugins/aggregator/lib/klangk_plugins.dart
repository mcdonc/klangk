// Native-capable stand-in for the devenv-generated klangk_plugins aggregator.
// Unlike native-plugins-stub (which returned []), this includes the real
// native-safe plugins so the desktop build ships working tools.
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_plugin_soliplex/klangk_plugin_soliplex.dart';
import 'package:klangk_plugin_xeyes/klangk_plugin_xeyes.dart';

List<ToolPlugin> createAllPlugins() => <ToolPlugin>[
      SoliplexPlugin(),
      XeyesPlugin(),
    ];
