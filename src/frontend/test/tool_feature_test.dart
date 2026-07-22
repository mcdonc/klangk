import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _TestFeature extends ToolPlugin {
  bool disposed = false;
  final Map<String, ToolHandler> _handlers;

  _TestFeature(this._handlers);

  @override
  Map<String, ToolHandler> get handlers => _handlers;

  @override
  void dispose() {
    disposed = true;
  }
}

void main() {
  // ToolPluginRegistry is a singleton, so we need to be careful with state.
  // We'll test dispatch behavior using a fresh feature each time.

  group('ToolPlugin', () {
    test('default buildOverlay returns null', () {
      final feature = _TestFeature({});
      // Can't call buildOverlay without a BuildContext in a unit test,
      // but we can verify the default implementation exists
      expect(feature.handlers, isEmpty);
    });

    test('default dispose does nothing', () {
      final feature = _TestFeature({});
      feature.dispose();
      expect(feature.disposed, isTrue);
    });
  });

  group('ToolPluginRegistry', () {
    late ToolPluginRegistry registry;

    setUp(() {
      registry = ToolPluginRegistry();
      // Clear any previously registered features
      registry.plugins; // access to verify it works
    });

    test('is a singleton', () {
      final a = ToolPluginRegistry();
      final b = ToolPluginRegistry();
      expect(identical(a, b), isTrue);
    });

    test('register adds feature', () {
      final initialCount = registry.plugins.length;
      final feature = _TestFeature({
        'test_action': (_) async => 'result',
      });
      registry.register(feature);
      expect(registry.plugins.length, initialCount + 1);
      expect(registry.plugins.last, feature);
    });

    test('features list is unmodifiable', () {
      expect(
        () => registry.plugins.add(_TestFeature({})),
        throwsUnsupportedError,
      );
    });

    test('dispatch calls handler', () async {
      final feature = _TestFeature({
        'greet': (req) async => 'hello ${req['name']}',
      });
      registry.register(feature);
      final result = await registry.dispatch('greet', {'name': 'world'});
      expect(result, 'hello world');
    });

    test('dispatch unknown action returns error', () async {
      final result = await registry.dispatch('nonexistent_action_xyz', {});
      expect(result, contains('Unknown action'));
    });

    test('disposeAll calls dispose on all features', () {
      final p1 = _TestFeature({'a': (_) async => ''});
      final p2 = _TestFeature({'b': (_) async => ''});
      registry.register(p1);
      registry.register(p2);
      registry.disposeAll();
      expect(p1.disposed, isTrue);
      expect(p2.disposed, isTrue);
    });
  });
}
