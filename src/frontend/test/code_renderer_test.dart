import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_highlight/flutter_highlight.dart';
import 'package:klangk_frontend/file_viewer/renderers/code_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

RenderableFile codeFile({
  String name = 'main.dart',
  String extension = 'dart',
  String text = 'void main() => print("hi");',
  bool fail = false,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    readText: () async {
      if (fail) throw Exception('boom');
      return text;
    },
    readBytes: () async => Uint8List(0),
    downloadUrl: 'http://x/$name',
  );
}

Future<void> pumpRenderer(WidgetTester tester, Widget child) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(body: SizedBox(width: 800, height: 600, child: child)),
    ),
  );
}

void main() {
  group('languageForExtension', () {
    test('maps known code extensions', () {
      expect(languageForExtension('dart'), 'dart');
      expect(languageForExtension('py'), 'python');
      expect(languageForExtension('ts'), 'typescript');
      expect(languageForExtension('yml'), 'yaml');
      expect(languageForExtension('sh'), 'bash');
      expect(languageForExtension('c'), 'cpp');
      expect(languageForExtension('html'), 'xml');
    });

    test('falls back to plaintext for unknown extensions', () {
      expect(languageForExtension('zzz'), 'plaintext');
      expect(languageForExtension(''), 'plaintext');
    });
  });

  group('CodeRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = CodeRenderer();
      expect(r.id, 'code');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.code);
      expect(r.priority, 10);
    });

    test('canRender matches code extensions only', () {
      final r = CodeRenderer();
      for (final ext in [
        'dart',
        'py',
        'ts',
        'js',
        'json',
        'yaml',
        'sh',
        'go'
      ]) {
        expect(r.canRender(codeFile(extension: ext)), isTrue, reason: ext);
      }
      expect(r.canRender(codeFile(extension: 'md')), isFalse);
      expect(r.canRender(codeFile(extension: 'png')), isFalse);
      expect(r.canRender(codeFile(extension: '')), isFalse);
    });
  });

  group('CodeRenderer build', () {
    testWidgets('shows a spinner before content resolves', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeRenderer().build(c, codeFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await tester.pumpAndSettle();
    });

    testWidgets('renders a highlighted code view', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeRenderer().build(c, codeFile())),
      );
      await tester.pumpAndSettle();
      expect(find.byType(HighlightView), findsOneWidget);
    });

    testWidgets('handles an unknown-but-registered code extension',
        (tester) async {
      // .go is in the code set; ensures the language path runs for it.
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => CodeRenderer().build(
            c,
            codeFile(name: 'main.go', extension: 'go', text: 'package main'),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(HighlightView), findsOneWidget);
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeRenderer().build(c, codeFile(fail: true))),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('Failed to load file'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('code is the default for code exts, raw still offered', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(codeFile());
      expect(renderers.first, isA<CodeRenderer>());
      expect(renderers.last.id, 'raw');
    });
  });
}
