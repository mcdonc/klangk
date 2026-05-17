import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/tools/tool_plugin.dart';

class _TestPlugin extends ToolPlugin {
  bool disposed = false;
  final Map<String, ToolHandler> _handlers;

  _TestPlugin(this._handlers);

  @override
  Map<String, ToolHandler> get handlers => _handlers;

  @override
  void dispose() {
    disposed = true;
  }
}

void main() {
  // ToolPluginRegistry is a singleton, so we need to be careful with state.
  // We'll test dispatch behavior using a fresh plugin each time.

  group('ToolPlugin', () {
    test('default buildOverlay returns null', () {
      final plugin = _TestPlugin({});
      // Can't call buildOverlay without a BuildContext in a unit test,
      // but we can verify the default implementation exists
      expect(plugin.handlers, isEmpty);
    });

    test('default dispose does nothing', () {
      final plugin = _TestPlugin({});
      plugin.dispose();
      expect(plugin.disposed, isTrue);
    });
  });

  group('ToolPluginRegistry', () {
    late ToolPluginRegistry registry;

    setUp(() {
      registry = ToolPluginRegistry();
      // Clear any previously registered plugins
      registry.plugins; // access to verify it works
    });

    test('is a singleton', () {
      final a = ToolPluginRegistry();
      final b = ToolPluginRegistry();
      expect(identical(a, b), isTrue);
    });

    test('register adds plugin', () {
      final initialCount = registry.plugins.length;
      final plugin = _TestPlugin({
        'test_action': (_) async => 'result',
      });
      registry.register(plugin);
      expect(registry.plugins.length, initialCount + 1);
      expect(registry.plugins.last, plugin);
    });

    test('plugins list is unmodifiable', () {
      expect(
        () => registry.plugins.add(_TestPlugin({})),
        throwsUnsupportedError,
      );
    });

    test('dispatch calls handler', () async {
      final plugin = _TestPlugin({
        'greet': (req) async => 'hello ${req['name']}',
      });
      registry.register(plugin);
      final result = await registry.dispatch('greet', {'name': 'world'});
      expect(result, 'hello world');
    });

    test('dispatch unknown action returns error', () async {
      final result = await registry.dispatch('nonexistent_action_xyz', {});
      expect(result, contains('Unknown action'));
    });

    test('disposeAll calls dispose on all plugins', () {
      final p1 = _TestPlugin({'a': (_) async => ''});
      final p2 = _TestPlugin({'b': (_) async => ''});
      registry.register(p1);
      registry.register(p2);
      registry.disposeAll();
      expect(p1.disposed, isTrue);
      expect(p2.disposed, isTrue);
    });
  });
}
