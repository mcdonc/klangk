import 'dart:typed_data';
import 'package:code_forge_web/code_forge_web.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/renderers/code_editor_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/code_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

RenderableFile codeFile({
  String name = 'main.dart',
  String extension = 'dart',
  String text = 'void main() {}',
  bool fail = false,
  Future<void> Function(String content)? saveText,
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
    saveText: saveText,
  );
}

Future<void> pumpRenderer(WidgetTester tester, Widget child) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(body: SizedBox(width: 800, height: 600, child: child)),
    ),
  );
}

/// The code editor runs a perpetual cursor-blink animation, so pumpAndSettle
/// never returns. Pump a few fixed frames to let futures/setState resolve.
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
  await tester.pump(const Duration(milliseconds: 50));
}

void main() {
  group('CodeEditorRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = CodeEditorRenderer();
      expect(r.id, 'edit');
      expect(r.modeLabel, 'Edit');
      expect(r.icon, Icons.edit);
      expect(r.priority, 5);
    });

    test('canRender matches code extensions', () {
      final r = CodeEditorRenderer();
      expect(r.canRender(codeFile(extension: 'dart')), isTrue);
      expect(r.canRender(codeFile(extension: 'py')), isTrue);
      expect(r.canRender(codeFile(extension: 'md')), isFalse);
      expect(r.canRender(codeFile(extension: 'png')), isFalse);
    });
  });

  group('CodeEditorRenderer build', () {
    testWidgets('shows a spinner before content resolves', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeEditorRenderer().build(c, codeFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await settle(tester);
    });

    testWidgets('mounts the editor with the loaded content', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeEditorRenderer().build(c, codeFile())),
      );
      await settle(tester);
      expect(find.byType(CodeForgeWeb), findsOneWidget);
    });

    testWidgets('shows read-only when no save path is available',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => CodeEditorRenderer().build(c, codeFile())),
      );
      await settle(tester);
      expect(find.text('read-only'), findsOneWidget);
      expect(find.widgetWithText(TextButton, 'Save'), findsNothing);
    });

    testWidgets('save persists the buffer and shows Saved', (tester) async {
      String? saved;
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => CodeEditorRenderer().build(
            c,
            codeFile(saveText: (content) async => saved = content),
          ),
        ),
      );
      await settle(tester);
      expect(find.widgetWithText(TextButton, 'Save'), findsOneWidget);

      await tester.tap(find.widgetWithText(TextButton, 'Save'));
      await settle(tester);
      expect(saved, 'void main() {}');
      expect(find.text('Saved'), findsOneWidget);
    });

    testWidgets('save failure surfaces an error message', (tester) async {
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => CodeEditorRenderer().build(
            c,
            codeFile(saveText: (content) async => throw Exception('nope')),
          ),
        ),
      );
      await settle(tester);
      await tester.tap(find.widgetWithText(TextButton, 'Save'));
      await settle(tester);
      expect(find.textContaining('Save failed'), findsOneWidget);
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => CodeEditorRenderer().build(c, codeFile(fail: true)),
        ),
      );
      await settle(tester);
      expect(find.textContaining('Failed to load file'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('code View is default, Edit and Raw also offered for code files', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(codeFile());
      expect(renderers.map((r) => r.id), ['code', 'edit', 'raw']);
      expect(isCodeExtension('dart'), isTrue);
    });
  });
}
