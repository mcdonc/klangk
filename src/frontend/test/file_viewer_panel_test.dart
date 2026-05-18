import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/file_viewer/file_viewer_panel.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

class _MockAguiClient extends AguiClient {
  final StreamController<AguiEvent> _controller =
      StreamController<AguiEvent>.broadcast();

  @override
  Stream<AguiEvent> get events => _controller.stream;

  void emit(AguiEvent event) => _controller.add(event);

  void close() => _controller.close();
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    // Mock HTTP client that returns empty file listings
    testHttpClientOverride = MockClient((request) async {
      if (request.url.path.contains('/files') &&
          !request.url.path.contains('/content')) {
        return http.Response(jsonEncode([]), 200);
      }
      return http.Response('Not found', 404);
    });
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testHttpClientOverride = null;
  });

  group('FileViewerPanel', () {
    testWidgets('renders with path bar', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('/'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
      expect(find.byIcon(Icons.folder), findsOneWidget);
      client.close();
    });

    testWidgets('has a refresh method', (tester) async {
      final client = _MockAguiClient();
      final key = GlobalKey<FileViewerPanelState>();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              key: key,
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      key.currentState!.refresh();
      await tester.pumpAndSettle();
      expect(find.byType(FileViewerPanel), findsOneWidget);
      client.close();
    });

    testWidgets('shows empty directory message', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Empty directory'), findsOneWidget);
      client.close();
    });

    testWidgets('shows file entries from mock', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'hello.txt',
                'path': 'hello.txt',
                'is_dir': false,
                'size': 11
              },
              {'name': 'src', 'path': 'src', 'is_dir': true, 'size': null},
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('hello.txt'), findsOneWidget);
      expect(find.text('src'), findsOneWidget);
      client.close();
    });

    testWidgets('refreshes on file_changed event', (tester) async {
      final client = _MockAguiClient();
      int callCount = 0;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          callCount++;
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final initialCalls = callCount;
      client.emit(AguiEvent(
        type: AguiEventType.custom,
        data: {'name': 'file_changed'},
      ));
      await tester.pumpAndSettle();

      expect(callCount, greaterThan(initialCalls));
      client.close();
    });

    testWidgets('clicking folder navigates into it', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        final path = request.url.queryParameters['path'] ?? '.';
        if (path == '.') {
          return http.Response(
            jsonEncode([
              {
                'name': 'subdir',
                'path': 'subdir',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        } else if (path == 'subdir') {
          return http.Response(
            jsonEncode([
              {
                'name': 'inner.txt',
                'path': 'subdir/inner.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Click on the folder
      await tester.tap(find.text('subdir'));
      await tester.pumpAndSettle();

      // Should now show the inner file
      expect(find.text('inner.txt'), findsOneWidget);
      client.close();
    });

    testWidgets('clicking file shows content', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/content')) {
          return http.Response(
            jsonEncode({'path': 'test.txt', 'content': 'file content here'}),
            200,
          );
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'test.txt',
                'path': 'test.txt',
                'is_dir': false,
                'size': 17
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('test.txt'));
      await tester.pumpAndSettle();

      expect(find.textContaining('file content here'), findsOneWidget);
      // Back button should appear
      expect(find.byIcon(Icons.arrow_back), findsOneWidget);
      client.close();
    });

    testWidgets('shows file sizes', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'big.txt',
                'path': 'big.txt',
                'is_dir': false,
                'size': 1024
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('1024'), findsOneWidget);
      client.close();
    });

    testWidgets('shows folder icon for directories', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {'name': 'mydir', 'path': 'mydir', 'is_dir': true, 'size': null},
              {'name': 'myfile', 'path': 'myfile', 'is_dir': false, 'size': 10},
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Should have folder icons for dirs and file icons for files
      expect(find.byIcon(Icons.folder), findsWidgets);
      expect(find.byIcon(Icons.insert_drive_file), findsOneWidget);
      client.close();
    });

    testWidgets('shows upload hint', (tester) async {
      final client = _MockAguiClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileViewerPanel(
              aguiClient: client,
              workspaceId: 'ws-1',
              authToken: 'token',
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('upload'), findsWidgets);
      client.close();
    });
  });
}
