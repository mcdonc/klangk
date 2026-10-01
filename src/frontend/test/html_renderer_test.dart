import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_highlight/flutter_highlight.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_frontend/file_viewer/renderers/html_renderer.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

RenderableFile htmlFile({
  String name = 'page.html',
  String extension = 'html',
  Future<String> Function()? readText,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    readText: readText ?? () async => '<h1>Title</h1>',
    readBytes: () async => Uint8List(0),
    downloadUrl: 'http://x/$name',
  );
}

FileRendererRegistry builtins() =>
    FileRendererRegistry()..registerAll(builtinFileRenderers());

Future<void> pump(WidgetTester tester, Widget child) => tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: SizedBox(width: 800, height: 600, child: child)),
      ),
    );

void main() {
  group('HtmlRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = HtmlRenderer();
      expect(r.id, 'html');
      expect(r.modeLabel, 'Preview');
      expect(r.icon, Icons.web);
      expect(r.priority, 20);
    });

    test('canRender matches html and htm only', () {
      final r = HtmlRenderer();
      expect(r.canRender(htmlFile()), isTrue);
      expect(r.canRender(htmlFile(name: 'p.htm', extension: 'htm')), isTrue);
      expect(r.canRender(htmlFile(name: 'p.xml', extension: 'xml')), isFalse);
    });
  });

  group('HtmlRenderer build', () {
    testWidgets('without a browser it shows the highlighted source',
        (tester) async {
      final file = htmlFile();
      await pump(
        tester,
        Builder(
          builder: (context) =>
              HtmlRenderer(previewSupported: false).build(context, file),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(HighlightView), findsOneWidget);
    });

    testWidgets('shows a spinner while the file loads', (tester) async {
      final pending = Completer<String>();
      final file = htmlFile(readText: () => pending.future);
      await pump(
        tester,
        Builder(
          builder: (context) =>
              HtmlRenderer(previewSupported: true).build(context, file),
        ),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('reports a file that fails to load', (tester) async {
      final file = htmlFile(readText: () async => throw Exception('boom'));
      await pump(
        tester,
        Builder(
          builder: (context) =>
              HtmlRenderer(previewSupported: true).build(context, file),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('Failed to load file'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('html renders by default for .html and .htm', () {
      for (final ext in ['html', 'htm']) {
        final renderers =
            builtins().renderersFor(htmlFile(name: 'p.$ext', extension: ext));
        expect(renderers.first.id, 'html', reason: '.$ext');
        expect(renderers.first.modeLabel, 'Preview', reason: '.$ext');
      }
    });

    test('the source stays one click away', () {
      final ids = builtins().renderersFor(htmlFile()).map((r) => r.id);
      expect(ids, containsAll(['html', 'code', 'raw']));
    });
  });
}
