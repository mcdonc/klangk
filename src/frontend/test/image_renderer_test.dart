import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/renderers/image_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

// A valid 1x1 transparent PNG.
final _validPng = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
);

RenderableFile imgFile({
  String name = 'pic.png',
  String extension = 'png',
  Uint8List? bytes,
  bool fail = false,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    readText: () async => '',
    readBytes: () async {
      if (fail) throw Exception('boom');
      return bytes ?? _validPng;
    },
    downloadUrl: 'http://x/$name',
  );
}

Future<void> pumpRenderer(WidgetTester tester, Widget child) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SizedBox(width: 800, height: 600, child: child),
      ),
    ),
  );
}

void main() {
  group('ImageRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = ImageRenderer();
      expect(r.id, 'image');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.image);
      expect(r.priority, 10);
    });

    test('canRender matches common raster extensions only', () {
      final r = ImageRenderer();
      for (final ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']) {
        expect(r.canRender(imgFile(extension: ext)), isTrue, reason: ext);
      }
      expect(r.canRender(imgFile(extension: 'txt')), isFalse);
      expect(r.canRender(imgFile(extension: 'svg')), isFalse);
    });
  });

  group('ImageRenderer build', () {
    testWidgets('shows a spinner before bytes resolve', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => ImageRenderer().build(c, imgFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await tester.pumpAndSettle();
    });

    testWidgets('renders the image with a rotate toolbar', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => ImageRenderer().build(c, imgFile())),
      );
      await tester.pumpAndSettle();
      expect(find.byType(Image), findsOneWidget);
      expect(find.byTooltip('Rotate'), findsOneWidget);
      expect(find.byType(RotatedBox), findsOneWidget);
    });

    testWidgets('rotate cycles quarterTurns 0->1->2->3->0', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => ImageRenderer().build(c, imgFile())),
      );
      await tester.pumpAndSettle();

      int turns() =>
          tester.widget<RotatedBox>(find.byType(RotatedBox)).quarterTurns;
      expect(turns(), 0);
      for (final expected in [1, 2, 3, 0]) {
        await tester.tap(find.byTooltip('Rotate'));
        await tester.pump();
        expect(turns(), expected);
      }
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => ImageRenderer().build(c, imgFile(fail: true))),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('Failed to load image'), findsOneWidget);
    });

    testWidgets('undecodable bytes fall back to a broken-image icon',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(
          builder: (c) => ImageRenderer().build(
            c,
            imgFile(bytes: Uint8List.fromList([1, 2, 3, 4, 5])),
          ),
        ),
      );
      await tester.pumpAndSettle();
      // Swallow the expected image-decode exception so it doesn't fail the test.
      tester.takeException();
      await tester.pumpAndSettle();
      expect(find.byIcon(Icons.broken_image), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('image is the default for png, raw still offered', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(imgFile());
      expect(renderers.first, isA<ImageRenderer>());
      expect(renderers.last.id, 'raw');
    });
  });
}
