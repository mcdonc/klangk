import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:pdfrx/pdfrx.dart';
import 'package:klangk_frontend/file_viewer/renderers/pdf_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

// A minimal PDF header (enough bytes for the widget; pdfium isn't loaded on the
// VM, so no real rendering happens — the widget mounts in a loading state).
final _pdfBytes = Uint8List.fromList('%PDF-1.4\n%%EOF\n'.codeUnits);

RenderableFile pdfFile({
  String name = 'doc.pdf',
  String extension = 'pdf',
  bool fail = false,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    readText: () async => '',
    readBytes: () async {
      if (fail) throw Exception('boom');
      return _pdfBytes;
    },
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

/// pdfium never loads on the VM, so the viewer animates indefinitely; pump
/// fixed frames instead of pumpAndSettle.
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 50));
  await tester.pump(const Duration(milliseconds: 50));
}

void main() {
  group('PdfRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = PdfRenderer();
      expect(r.id, 'pdf');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.picture_as_pdf);
      expect(r.priority, 10);
    });

    test('canRender matches pdf only', () {
      final r = PdfRenderer();
      expect(r.canRender(pdfFile(extension: 'pdf')), isTrue);
      expect(r.canRender(pdfFile(extension: 'txt')), isFalse);
      expect(r.canRender(pdfFile(extension: '')), isFalse);
    });
  });

  group('PdfRenderer build', () {
    testWidgets('shows a spinner before bytes resolve', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => PdfRenderer().build(c, pdfFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await settle(tester);
    });

    testWidgets('mounts the pdf viewer with page navigation', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => PdfRenderer().build(c, pdfFile())),
      );
      await settle(tester);
      expect(find.byType(PdfViewer), findsOneWidget);
      expect(find.text('Page 1'), findsOneWidget);
      expect(find.byTooltip('Next page'), findsOneWidget);
      expect(find.byTooltip('Previous page'), findsOneWidget);
    });

    testWidgets('next/previous page updates the page indicator',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => PdfRenderer().build(c, pdfFile())),
      );
      await settle(tester);

      await tester.tap(find.byTooltip('Next page'));
      await settle(tester);
      expect(find.text('Page 2'), findsOneWidget);

      await tester.tap(find.byTooltip('Previous page'));
      await settle(tester);
      expect(find.text('Page 1'), findsOneWidget);

      // Previous from page 1 is clamped (stays at 1).
      await tester.tap(find.byTooltip('Previous page'));
      await settle(tester);
      expect(find.text('Page 1'), findsOneWidget);
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => PdfRenderer().build(c, pdfFile(fail: true))),
      );
      await settle(tester);
      expect(find.textContaining('Failed to load PDF'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('pdf is the default for .pdf', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(pdfFile());
      expect(renderers.first, isA<PdfRenderer>());
    });
  });
}
