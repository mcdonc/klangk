import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:klangk_frontend/file_viewer/renderers/markdown_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_frontend/file_viewer/renderers/raw_text_renderer.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

RenderableFile mdFile({
  String name = 'doc.md',
  String extension = 'md',
  String text = '# Title\n\nSome **bold** text.\n\n- one\n- two\n',
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
        home: Scaffold(body: SizedBox(width: 800, height: 600, child: child))),
  );
}

void main() {
  group('MarkdownRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = MarkdownRenderer();
      expect(r.id, 'markdown');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.article);
      expect(r.priority, 10);
    });

    test('canRender matches md and markdown only', () {
      final r = MarkdownRenderer();
      expect(r.canRender(mdFile(extension: 'md')), isTrue);
      expect(r.canRender(mdFile(extension: 'markdown')), isTrue);
      expect(r.canRender(mdFile(extension: 'txt')), isFalse);
      expect(r.canRender(mdFile(extension: '')), isFalse);
    });
  });

  group('handleMarkdownLink', () {
    test('non-empty href is accepted (opens via web helper)', () {
      // openUrl is a no-op stub on the VM; just exercise both branches.
      expect(() => handleMarkdownLink('text', 'https://example.test', null),
          returnsNormally);
    });

    test('null or empty href is ignored', () {
      expect(() => handleMarkdownLink('text', null, null), returnsNormally);
      expect(() => handleMarkdownLink('text', '', null), returnsNormally);
    });
  });

  group('MarkdownRenderer build', () {
    testWidgets('shows a spinner before content resolves', (tester) async {
      // First frame (pumpWidget): the readText future is still pending.
      await pumpRenderer(
        tester,
        Builder(builder: (c) => MarkdownRenderer().build(c, mdFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await tester.pumpAndSettle();
    });

    testWidgets('renders headings, lists, and bold text', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => MarkdownRenderer().build(c, mdFile())),
      );
      await tester.pumpAndSettle();
      // Rich markdown is rendered (Markdown widget present, not raw source).
      expect(find.byType(Markdown), findsOneWidget);
      expect(find.textContaining('Title'), findsWidgets);
      expect(find.textContaining('one'), findsWidgets);
    });

    testWidgets('renders a link', (tester) async {
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => MarkdownRenderer().build(
            c,
            mdFile(text: '[klangk](https://example.test)'),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(Markdown), findsOneWidget);
      expect(find.textContaining('klangk'), findsWidgets);
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(
            builder: (c) => MarkdownRenderer().build(c, mdFile(fail: true))),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('Failed to load file'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('markdown is the default for .md, raw still offered', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(mdFile());
      expect(renderers.first, isA<MarkdownRenderer>());
      expect(renderers.whereType<RawTextRenderer>(), hasLength(1));
    });

    test('a plain .txt still falls back to raw only', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers =
          registry.renderersFor(mdFile(name: 'x.txt', extension: 'txt'));
      expect(renderers, hasLength(1));
      expect(renderers.single, isA<RawTextRenderer>());
    });
  });
}
