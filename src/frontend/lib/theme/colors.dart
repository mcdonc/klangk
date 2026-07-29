// coverage:ignore-file
// KColors lives in klangk_plugin_api (single source of truth, #1976) so that
// compiled-in feature packages share the host's design tokens without
// importing the host app (which would close a package cycle). Re-exported
// here so the existing `import '../theme/colors.dart'` usages across the
// frontend keep working unchanged.
export 'package:klangk_plugin_api/klangk_plugin_api.dart' show KColors;
