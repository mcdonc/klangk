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
            {'name': 'note.txt', 'path': 'docs/note.txt', 'is_dir': false},
          ]),
          200,
        );
      }
      return http.Response('nf', 404);
    });

Widget _ide(GlobalKey<FileViewerPanelState> fvKey, WsClient ws, String? file) =>
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
            ),
            terminal: const SizedBox(),
            initialFile: file,
          ),
        ),
      ),
    );

void main() {
  tearDown(() {
    testBaseUrlOverride = null;
    testHttpClientOverride = null;
  });

  testWidgets('initialFile opens the file in the Files tab on load',
      (tester) async {
    testBaseUrlOverride = 'http://localhost:8997';
    testHttpClientOverride = _client();
    final fvKey = GlobalKey<FileViewerPanelState>();
    final ws = _MockWsClient();
    await tester.pumpWidget(_ide(fvKey, ws, 'docs/note.txt'));
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
    await tester.pumpWidget(_ide(fvKey, ws, 'docs/note.txt'));
    await tester.pumpAndSettle();
    expect(find.textContaining('ide body'), findsOneWidget);
    ws.close();
  });
}
