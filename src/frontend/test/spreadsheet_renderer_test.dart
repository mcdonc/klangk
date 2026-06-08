import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sheetifye/sheetifye.dart';
import 'package:klangk_frontend/file_viewer/renderers/spreadsheet_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

// A few bytes are enough for the widget to mount; sheetifye parses xlsx in a
// background isolate, so the renderer's build path is exercised regardless of
// whether these bytes parse into a real workbook.
final _xlsxBytes = Uint8List.fromList(List<int>.filled(32, 0));

RenderableFile sheetFile({
  String name = 'book.xlsx',
  String extension = 'xlsx',
  bool fail = false,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    readText: () async => '',
    readBytes: () async {
      if (fail) throw Exception('boom');
      return _xlsxBytes;
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

/// sheetifye shows an animated shimmer while parsing, so pumpAndSettle would
/// hang — pump fixed frames instead.
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 20));
  await tester.pump(const Duration(milliseconds: 20));
}

void main() {
  group('SpreadsheetRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = SpreadsheetRenderer();
      expect(r.id, 'spreadsheet');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.table_chart);
      expect(r.priority, 10);
    });

    test('canRender matches xlsx only (legacy xls falls through)', () {
      final r = SpreadsheetRenderer();
      expect(r.canRender(sheetFile(extension: 'xlsx')), isTrue);
      expect(r.canRender(sheetFile(extension: 'xls')), isFalse);
      expect(r.canRender(sheetFile(extension: 'txt')), isFalse);
      expect(r.canRender(sheetFile(extension: '')), isFalse);
    });
  });

  group('SpreadsheetRenderer build', () {
    testWidgets('shows a spinner before bytes resolve', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => SpreadsheetRenderer().build(c, sheetFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await settle(tester);
    });

    testWidgets('mounts the sheetifye widget under a ProviderScope once ready',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => SpreadsheetRenderer().build(c, sheetFile())),
      );
      await settle(tester);
      expect(find.byType(Sheetifye), findsOneWidget);
      expect(find.byType(ProviderScope), findsOneWidget);
      // Tear down to dispose sheetifye cleanly.
      await tester.pumpWidget(const SizedBox());
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => SpreadsheetRenderer().build(c, sheetFile(fail: true)),
        ),
      );
      await settle(tester);
      expect(find.textContaining('Failed to load spreadsheet'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('spreadsheet is the default for .xlsx', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(sheetFile());
      expect(renderers.first, isA<SpreadsheetRenderer>());
    });
  });
}
