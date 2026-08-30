import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/file_viewer/file_viewer_panel.dart';
import 'package:klangk_frontend/layout/ide_layout.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart'
    show testBaseUrlOverride;

class _MockWsClient extends WsClient {
  final _controller = StreamController<Map<String, dynamic>>.broadcast();
  @override
  Stream<Map<String, dynamic>> get customEvents => _controller.stream;
  void close() => _controller.close();
}

/// Serves a one-file listing + text content for any path.
MockClient _client() => MockClient((req) async {
      if (req.url.path.contains('/files/content')) {
        return http.Response(jsonEncode({'content': 'ide body'}), 200);
      }
      if (req.url.path.contains('/files')) {
        return http.Response(
          jsonEncode([
            {
              'name': 'note.txt',
              'path': '/home/docs/note.txt',
              'is_dir': false
            },
          ]),
          200,
        );
      }
      return http.Response('nf', 404);
    });

Widget _ide(GlobalKey<FileViewerPanelState> fvKey, WsClient ws, String? file,
        {String? dir}) =>
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1000,
          height: 700,
          child: IdeLayout(
            fileViewerKey: fvKey,
            fileViewer: FileViewerPanel(
              key: fvKey,
              wsClient: ws,
              workspaceId: 'ws-1',
              authToken: 'tok',
              userHome: '/home/tester',
              canDownload: true,
              canWrite: true,
            ),
            terminal: const SizedBox(),
            initialFile: file,
            initialDir: dir,
          ),
        ),
      ),
    );

/// Rebuilds IdeLayout with the Files pane granted mid-session — the shape
/// of the permissions-fetch race where `files` lands after the first build
/// (#2886).
class _LatePaneHarness extends StatefulWidget {
  const _LatePaneHarness({
    super.key,
    required this.fvKey,
    required this.ws,
    this.initialFile,
  });
  final GlobalKey<FileViewerPanelState> fvKey;
  final WsClient ws;
  final String? initialFile;
  @override
  State<_LatePaneHarness> createState() => _LatePaneHarnessState();
}

class _LatePaneHarnessState extends State<_LatePaneHarness> {
  bool _showFiles = false;
  void grantFiles() => setState(() => _showFiles = true);
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1000,
          height: 700,
          child: IdeLayout(
            fileViewerKey: widget.fvKey,
            fileViewer: _showFiles
                ? FileViewerPanel(
                    key: widget.fvKey,
                    wsClient: widget.ws,
                    workspaceId: 'ws-1',
                    authToken: 'tok',
                    userHome: '/home/tester',
                    canDownload: true,
                    canWrite: true,
                  )
                : null,
            terminal: const Text('TERMINAL_CONTENT'),
            initialFile: widget.initialFile,
          ),
        ),
      ),
    );
  }
}

void main() {
  setUp(clearFileListCacheForTest);
  tearDown(() {
    testBaseUrlOverride = null;
    testHttpClientOverride = null;
    clearFileListCacheForTest();
  });

  testWidgets('initialFile opens the file in the Files tab on load',
      (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, '/home/docs/note.txt'));
    await tester.pumpAndSettle();
    expect(find.textContaining('ide body'), findsOneWidget);
    ws.close();
  });

  testWidgets('no initialFile leaves the file unopened', (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, null));
    await tester.pumpAndSettle();
    expect(find.textContaining('ide body'), findsNothing);
    ws.close();
  });

  testWidgets('changing initialFile reopens (didUpdateWidget)', (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, null));
    await tester.pumpAndSettle();
    expect(find.textContaining('ide body'), findsNothing);
    // Same tree, new initialFile → IdeLayout.didUpdateWidget fires.
    await tester.pumpWidget(_ide(fvKey, ws, '/home/docs/note.txt'));
    await tester.pumpAndSettle();
    expect(find.textContaining('ide body'), findsOneWidget);
    ws.close();
  });

  testWidgets('initialDir browses the folder on load (no file opened)',
      (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, null, dir: '/home/docs'));
    await tester.pumpAndSettle();
    // Breadcrumb segment proves openDirectory('docs') ran; no file content.
    expect(find.text('docs'), findsOneWidget);
    expect(find.textContaining('ide body'), findsNothing);
    ws.close();
  });

  testWidgets('changing initialDir reopens the folder (didUpdateWidget)',
      (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, null));
    await tester.pumpAndSettle();
    expect(find.text('docs'), findsNothing);
    // Same tree, new initialDir → IdeLayout.didUpdateWidget fires.
    await tester.pumpWidget(_ide(fvKey, ws, null, dir: '/home/docs'));
    await tester.pumpAndSettle();
    expect(find.text('docs'), findsOneWidget);
    ws.close();
  });

  testWidgets(
      'initialFile no-ops without a Files pane (no files permission, #2886)',
      (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 1000,
            height: 700,
            child: IdeLayout(
              fileViewer: null,
              terminal: const Text('TERMINAL_CONTENT'),
              initialFile: '/home/docs/note.txt',
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    // No crash, no tab switch off Terminal, no Files tab to switch to.
    expect(find.text('Files'), findsNothing);
    expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
  });

  testWidgets(
      'pending initialFile opens once the pane arrives late (permissions race)',
      (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    final harnessKey = GlobalKey<_LatePaneHarnessState>();
    await tester.pumpWidget(
      _LatePaneHarness(
        key: harnessKey,
        fvKey: fvKey,
        ws: ws,
        initialFile: '/home/docs/note.txt',
      ),
    );
    await tester.pumpAndSettle();
    // First build has no Files pane: the deep-link waits, nothing opens.
    expect(find.text('ide body'), findsNothing);

    harnessKey.currentState!.grantFiles();
    await tester.pumpAndSettle();

    // The pane's arrival re-runs the pending deep-link.
    expect(find.text('ide body'), findsOneWidget);
    ws.close();
  });
}
