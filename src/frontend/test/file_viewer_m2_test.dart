import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/file_viewer/file_viewer_panel.dart';
import 'package:klangk_frontend/file_viewer/file_renderer_wiring.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_frontend/file_viewer/renderers/raw_text_renderer.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _controller =
      StreamController<Map<String, dynamic>>.broadcast();

  @override
  Stream<Map<String, dynamic>> get customEvents => _controller.stream;

  void close() => _controller.close();
}

/// Fake "View" renderer that reads the file's BYTES (exercising the binary
/// loader path that image/pdf renderers use later) and matches `.md` files.
class _BytesViewRenderer extends FileRenderer {
  @override
  String get id => 'bytesview';
  @override
  String get modeLabel => 'View';
  @override
  IconData get icon => Icons.visibility;
  @override
  int get priority => 10;
  @override
  bool canRender(RenderableFile file) => file.extension == 'md';
  @override
  Widget build(BuildContext context, RenderableFile file) {
    return FutureBuilder<List<int>>(
      future: file.readBytes(),
      builder: (context, snap) {
        if (snap.hasError) return const Text('BYTES-ERROR');
        if (!snap.hasData) return const Text('BYTES-LOADING');
        return Text('BYTES:${snap.data!.length}');
      },
    );
  }
}

/// A renderer that invokes the injected [RenderableFile.saveText] on tap, to
/// exercise the panel's save path (`_saveFileText`).
class _SaveProbeRenderer extends FileRenderer {
  @override
  String get id => 'probe';
  @override
  String get modeLabel => 'Probe';
  @override
  IconData get icon => Icons.save;
  @override
  int get priority => 20;
  @override
  bool canRender(RenderableFile file) => true;
  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _SaveProbe(file: file);
}

class _SaveProbe extends StatefulWidget {
  const _SaveProbe({required this.file});
  final RenderableFile file;
  @override
  State<_SaveProbe> createState() => _SaveProbeState();
}

class _SaveProbeState extends State<_SaveProbe> {
  String _result = 'idle';
  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        TextButton(
          onPressed: () async {
            try {
              await widget.file.saveText!('NEWBODY');
              if (mounted) setState(() => _result = 'ok');
            } catch (_) {
              if (mounted) setState(() => _result = 'err');
            }
          },
          child: const Text('dosave'),
        ),
        Text(_result),
      ],
    );
  }
}

/// A ToolPlugin that also contributes file renderers.
class _BothFeature extends ToolPlugin implements FileRendererPlugin {
  @override
  Map<String, ToolHandler> get handlers => {};
  @override
  List<FileRenderer> get fileRenderers => [_BytesViewRenderer()];
}

/// A plain ToolPlugin with no renderers.
class _ToolOnlyFeature extends ToolPlugin {
  @override
  Map<String, ToolHandler> get handlers => {};
}

/// Pumps a [FileViewerPanel] listing a single file with [name]/[path],
/// optionally with a custom [registry].
Future<_MockWsClient> _pumpPanel(
  WidgetTester tester, {
  required MockClient client,
  String name = 'note.md',
  String path = 'note.md',
  FileRendererRegistry? registry,
}) async {
  testBaseUrlOverride = 'http://localhost:8997';
  testHttpClientOverride = client;
  clearFileListCacheForTest();
  final ws = _MockWsClient();
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 900,
          height: 600,
          child: FileViewerPanel(
            wsClient: ws,
            workspaceId: 'ws-1',
            authToken: 'token',
            registry: registry,
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return ws;
}

MockClient _listing(List<Map<String, dynamic>> entries,
    {String? content, int contentStatus = 200, int downloadStatus = 200}) {
  return MockClient((request) async {
    if (request.url.path.contains('/files/content')) {
      return http.Response(
          jsonEncode({'content': content ?? 'plain text body'}), contentStatus);
    }
    if (request.url.path.contains('/files/download')) {
      return http.Response('BYTESDATA', downloadStatus);
    }
    if (request.url.path.contains('/files')) {
      return http.Response(jsonEncode(entries), 200);
    }
    return http.Response('Not found', 404);
  });
}

/// Renders a unique marker for one extension, to prove which renderer the
/// viewer selected (so a stale renderer after a direct file switch is visible).
class _MarkerRenderer extends FileRenderer {
  _MarkerRenderer(this.ext, this.marker);
  final String ext;
  final String marker;
  @override
  String get id => marker;
  @override
  String get modeLabel => marker;
  @override
  IconData get icon => Icons.abc;
  @override
  int get priority => 10;
  @override
  bool canRender(RenderableFile file) => file.extension == ext;
  @override
  Widget build(BuildContext context, RenderableFile file) => Text(marker);
}

void main() {
  tearDown(() {
    testBaseUrlOverride = null;
    testHttpClientOverride = null;
    clearFileListCacheForTest();
  });

  group('RawTextRenderer', () {
    test('metadata: always renders, lowest priority, raw id', () {
      final r = RawTextRenderer();
      expect(r.id, 'raw');
      expect(r.modeLabel, 'Raw');
      expect(r.priority, 1);
      expect(r.icon, Icons.subject);
      expect(
        r.canRender(RenderableFile(
          path: 'x',
          name: 'x',
          extension: '',
          readText: () async => '',
          readBytes: () async => Uint8List(0),
          downloadUrl: '',
        )),
        isTrue,
      );
    });

    testWidgets('shows a loading spinner before content resolves',
        (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 4},
        ]),
        registry: FileRendererRegistry()..register(RawTextRenderer()),
      );
      await tester.tap(find.text('note.md'));
      await tester.pump(); // one frame: future still pending
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await tester.pumpAndSettle();
      ws.close();
    });

    testWidgets('renders decoded text content', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'a.txt', 'path': 'a.txt', 'is_dir': false, 'size': 4},
        ], content: 'hello raw world'),
        name: 'a.txt',
        path: 'a.txt',
      );
      await tester.tap(find.text('a.txt'));
      await tester.pumpAndSettle();
      expect(find.textContaining('hello raw world'), findsOneWidget);
      ws.close();
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'a.txt', 'path': 'a.txt', 'is_dir': false, 'size': 4},
        ], contentStatus: 500),
      );
      await tester.tap(find.text('a.txt'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Failed to load file'), findsOneWidget);
      ws.close();
    });
  });

  group('builtinFileRenderers', () {
    test('includes exactly one raw fallback, last', () {
      final renderers = builtinFileRenderers();
      expect(renderers, isNotEmpty);
      expect(renderers.last, isA<RawTextRenderer>());
      expect(renderers.whereType<RawTextRenderer>(), hasLength(1));
    });
  });

  group('buildFileRendererRegistry', () {
    test('registers builtins plus FileRendererPlugin renderers', () {
      final registry = buildFileRendererRegistry([
        _ToolOnlyFeature(),
        _BothFeature(),
      ]);
      final md = RenderableFile(
        path: 'x.md',
        name: 'x.md',
        extension: 'md',
        readText: () async => '',
        readBytes: () async => Uint8List(0),
        downloadUrl: '',
      );
      final ids = registry.renderersFor(md).map((r) => r.id).toList();
      // The feature's renderer is registered alongside the builtins, and the
      // raw fallback remains available.
      expect(ids, contains('bytesview'));
      expect(ids, contains('raw'));
    });

    test('with no features, offers only builtins', () {
      final registry = buildFileRendererRegistry([]);
      final txt = RenderableFile(
        path: 'x.txt',
        name: 'x.txt',
        extension: 'txt',
        readText: () async => '',
        readBytes: () async => Uint8List(0),
        downloadUrl: '',
      );
      expect(registry.renderersFor(txt).map((r) => r.id), ['raw']);
    });
  });

  group('_FileViewer chrome', () {
    FileRendererRegistry multiRegistry() => FileRendererRegistry()
      ..register(_BytesViewRenderer())
      ..register(RawTextRenderer());

    testWidgets('defaults to the highest-priority renderer and reads bytes',
        (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
        ]),
        registry: multiRegistry(),
      );
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();
      // 'BYTESDATA' is 9 bytes.
      expect(find.text('BYTES:9'), findsOneWidget);
      // Mode chips appear (2 renderers).
      expect(find.widgetWithText(ChoiceChip, 'View'), findsOneWidget);
      expect(find.widgetWithText(ChoiceChip, 'Raw'), findsOneWidget);
      ws.close();
    });

    testWidgets('view-raw button switches to the raw renderer', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
        ], content: 'raw markdown source'),
        registry: multiRegistry(),
      );
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();
      expect(find.text('BYTES:9'), findsOneWidget);

      await tester.tap(find.byTooltip('View raw'));
      await tester.pumpAndSettle();
      expect(find.textContaining('raw markdown source'), findsOneWidget);
      // Now raw is selected → the view-raw button disappears.
      expect(find.byTooltip('View raw'), findsNothing);
      ws.close();
    });

    testWidgets('mode chips switch between renderers', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
        ], content: 'raw body text'),
        registry: multiRegistry(),
      );
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();

      // Switch to Raw via chip.
      await tester.tap(find.widgetWithText(ChoiceChip, 'Raw'));
      await tester.pumpAndSettle();
      expect(find.textContaining('raw body text'), findsOneWidget);

      // Switch back to View via chip.
      await tester.tap(find.widgetWithText(ChoiceChip, 'View'));
      await tester.pumpAndSettle();
      expect(find.text('BYTES:9'), findsOneWidget);
      ws.close();
    });

    testWidgets('single renderer (no raw): no chips, no view-raw button',
        (tester) async {
      // Registry with only the bytes renderer → one match, no raw fallback.
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
        ]),
        registry: FileRendererRegistry()..register(_BytesViewRenderer()),
      );
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();
      expect(find.text('BYTES:9'), findsOneWidget);
      expect(find.byType(ChoiceChip), findsNothing);
      expect(find.byTooltip('View raw'), findsNothing);
      ws.close();
    });

    testWidgets('download button fires a download request', (tester) async {
      var downloadHit = false;
      final client = MockClient((request) async {
        if (request.url.path.contains('/files/download')) {
          downloadHit = true;
          return http.Response('BYTESDATA', 200);
        }
        if (request.url.path.contains('/files/content')) {
          return http.Response(jsonEncode({'content': 'x'}), 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {'name': 'a.txt', 'path': 'work/a.txt', 'is_dir': false}
            ]),
            200,
          );
        }
        return http.Response('nf', 404);
      });
      final ws = await _pumpPanel(tester, client: client);
      await tester.tap(find.text('a.txt'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Download'));
      await tester.pumpAndSettle();
      expect(downloadHit, isTrue);
      ws.close();
    });

    testWidgets('byte read error surfaces in the renderer', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
        ], downloadStatus: 404),
        registry: FileRendererRegistry()..register(_BytesViewRenderer()),
      );
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();
      expect(find.text('BYTES-ERROR'), findsOneWidget);
      ws.close();
    });

    testWidgets('back button clears the viewer', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'a.txt', 'path': 'a.txt', 'is_dir': false, 'size': 4},
        ], content: 'goodbye'),
      );
      await tester.tap(find.text('a.txt'));
      await tester.pumpAndSettle();
      expect(find.textContaining('goodbye'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();
      expect(find.textContaining('goodbye'), findsNothing);
      // Back to the file list.
      expect(find.text('a.txt'), findsOneWidget);
      ws.close();
    });

    testWidgets('opening a different file resets to its default renderer',
        (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'note.md', 'path': 'note.md', 'is_dir': false, 'size': 9},
          {
            'name': 'plain.txt',
            'path': 'plain.txt',
            'is_dir': false,
            'size': 4
          },
        ], content: 'plain text content'),
        registry: multiRegistry(),
      );
      // Open the .md file → defaults to View (bytes renderer).
      await tester.tap(find.text('note.md'));
      await tester.pumpAndSettle();
      expect(find.text('BYTES:9'), findsOneWidget);

      // Close, then open the .txt file → only raw matches → raw shown.
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();
      await tester.tap(find.text('plain.txt'));
      await tester.pumpAndSettle();
      expect(find.textContaining('plain text content'), findsOneWidget);
      expect(find.byType(ChoiceChip), findsNothing); // only raw matches .txt
      ws.close();
    });
  });

  group('save path (_saveFileText via injected saveText)', () {
    testWidgets('posts edited content to /files/upload on success',
        (tester) async {
      String? uploadPath;
      final client = MockClient((request) async {
        if (request.url.path.contains('/files/upload')) {
          uploadPath = request.url.queryParameters['path'];
          return http.Response('{"status":"uploaded"}', 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {'name': 'a.dart', 'path': 'work/a.dart', 'is_dir': false},
            ]),
            200,
          );
        }
        return http.Response('nf', 404);
      });
      final ws = await _pumpPanel(
        tester,
        client: client,
        registry: FileRendererRegistry()..register(_SaveProbeRenderer()),
      );
      await tester.tap(find.text('a.dart'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('dosave'));
      await tester.pumpAndSettle();
      expect(find.text('ok'), findsOneWidget);
      expect(uploadPath, 'work/a.dart');
      ws.close();
    });

    testWidgets('throws on a non-200 upload response', (tester) async {
      final client = MockClient((request) async {
        if (request.url.path.contains('/files/upload')) {
          return http.Response('boom', 500);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {'name': 'a.dart', 'path': 'work/a.dart', 'is_dir': false},
            ]),
            200,
          );
        }
        return http.Response('nf', 404);
      });
      final ws = await _pumpPanel(
        tester,
        client: client,
        registry: FileRendererRegistry()..register(_SaveProbeRenderer()),
      );
      await tester.tap(find.text('a.dart'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('dosave'));
      await tester.pumpAndSettle();
      expect(find.text('err'), findsOneWidget);
      ws.close();
    });
  });

  group('_renderableFor extension parsing', () {
    testWidgets('file without extension still opens (raw)', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': 'Makefile', 'path': 'work/Makefile', 'is_dir': false},
        ], content: 'all: build'),
      );
      await tester.tap(find.text('Makefile'));
      await tester.pumpAndSettle();
      expect(find.textContaining('all: build'), findsOneWidget);
      ws.close();
    });

    testWidgets('dotfile (leading dot only) opens as raw', (tester) async {
      final ws = await _pumpPanel(
        tester,
        client: _listing([
          {'name': '.bashrc', 'path': 'work/.bashrc', 'is_dir': false},
        ], content: 'export X=1'),
      );
      await tester.tap(find.text('.bashrc'));
      await tester.pumpAndSettle();
      expect(find.textContaining('export X=1'), findsOneWidget);
      ws.close();
    });
  });

  group('openFile (deep-link entry point)', () {
    testWidgets('positions at the file dir and shows its content',
        (tester) async {
      testBaseUrlOverride = 'http://localhost:8997';
      testHttpClientOverride = _listing([
        {'name': 'note.txt', 'path': 'docs/note.txt', 'is_dir': false},
      ], content: 'deep-linked body');
      final key = GlobalKey<FileViewerPanelState>();
      final ws = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 900,
            height: 600,
            child: FileViewerPanel(
              key: key,
              wsClient: ws,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      ));
      await tester.pumpAndSettle();
      key.currentState!.openFile('docs/note.txt');
      await tester.pumpAndSettle();
      expect(find.textContaining('deep-linked body'), findsOneWidget);
      ws.close();
    });
  });

  group('openDir + direct file switch', () {
    testWidgets('openDir browses the folder (shows the listing)',
        (tester) async {
      testBaseUrlOverride = 'http://localhost:8997';
      testHttpClientOverride = _listing([
        {'name': 'note.txt', 'path': 'docs/note.txt', 'is_dir': false},
      ]);
      final key = GlobalKey<FileViewerPanelState>();
      final ws = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 900,
            height: 600,
            child: FileViewerPanel(
                key: key, wsClient: ws, workspaceId: 'ws-1', authToken: 'tok'),
          ),
        ),
      ));
      await tester.pumpAndSettle();
      key.currentState!.openDir('docs');
      await tester.pumpAndSettle();
      expect(
          find.text('note.txt'), findsOneWidget); // the listing, not a viewer
      ws.close();
    });

    testWidgets(
        'switching files directly recreates the viewer with the new renderer',
        (tester) async {
      testBaseUrlOverride = 'http://localhost:8997';
      testHttpClientOverride = _listing([
        {'name': 'a.md', 'path': 'docs/a.md', 'is_dir': false},
        {'name': 'b.txt', 'path': 'docs/b.txt', 'is_dir': false},
      ]);
      final registry = FileRendererRegistry()
        ..registerAll([
          _MarkerRenderer('md', 'MD_RENDERER'),
          _MarkerRenderer('txt', 'TXT_RENDERER'),
        ]);
      final key = GlobalKey<FileViewerPanelState>();
      final ws = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 900,
            height: 600,
            child: FileViewerPanel(
              key: key,
              wsClient: ws,
              workspaceId: 'ws-1',
              authToken: 'tok',
              registry: registry,
            ),
          ),
        ),
      ));
      await tester.pumpAndSettle();
      key.currentState!.openFile('docs/a.md');
      await tester.pumpAndSettle();
      expect(find.text('MD_RENDERER'), findsOneWidget);
      // Switch directly to a different-type file (no list step in between).
      key.currentState!.openFile('docs/b.txt');
      await tester.pumpAndSettle();
      expect(find.text('TXT_RENDERER'), findsOneWidget);
      expect(find.text('MD_RENDERER'), findsNothing);
      ws.close();
    });
  });
}
