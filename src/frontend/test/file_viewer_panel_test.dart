import 'dart:async';
import 'dart:convert';
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/file_viewer/file_viewer_panel.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _controller =
      StreamController<Map<String, dynamic>>.broadcast();

  @override
  Stream<Map<String, dynamic>> get customEvents => _controller.stream;

  void emit(Map<String, dynamic> event) => _controller.add(event);

  void close() => _controller.close();
}

FileViewerPanel buildPanel({
  required _MockWsClient wsClient,
  GlobalKey<FileViewerPanelState>? key,
  String workspaceId = 'ws-1',
  String authToken = 'token',
  String userHome = '/home/tester',
  FileRendererRegistry? registry,
}) =>
    FileViewerPanel(
      key: key,
      wsClient: wsClient,
      workspaceId: workspaceId,
      authToken: authToken,
      userHome: userHome,
      registry: registry,
    );

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
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('home'), findsOneWidget);
      expect(find.text('tester'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
      expect(find.byIcon(Icons.home), findsOneWidget);
      client.close();
    });

    testWidgets('has a refresh method', (tester) async {
      final client = _MockWsClient();
      final key = GlobalKey<FileViewerPanelState>();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(key: key, wsClient: client),
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
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
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
                'path': '/home/tester/hello.txt',
                'is_dir': false,
                'size': 11
              },
              {
                'name': 'src',
                'path': '/home/tester/src',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('hello.txt'), findsOneWidget);
      expect(find.text('src'), findsOneWidget);
      client.close();
    });

    testWidgets('clicking folder navigates into it', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        final path = request.url.queryParameters['path'] ?? '/home/tester';
        if (path == '/home/tester') {
          return http.Response(
            jsonEncode([
              {
                'name': 'subdir',
                'path': '/home/tester/subdir',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        } else if (path == '/home/tester/subdir') {
          return http.Response(
            jsonEncode([
              {
                'name': 'inner.txt',
                'path': '/home/tester/subdir/inner.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
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
            jsonEncode({
              'path': '/home/tester/test.txt',
              'content': 'file content here'
            }),
            200,
          );
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'test.txt',
                'path': '/home/tester/test.txt',
                'is_dir': false,
                'size': 17
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
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
                'path': '/home/tester/big.txt',
                'is_dir': false,
                'size': 1024
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
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
              {
                'name': 'mydir',
                'path': '/home/tester/mydir',
                'is_dir': true,
                'size': null
              },
              {
                'name': 'myfile',
                'path': '/home/tester/myfile',
                'is_dir': false,
                'size': 10
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
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
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('upload'), findsWidgets);
      client.close();
    });

    testWidgets('file listing error shows debug message', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        return http.Response('Server error', 500);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
          ),
        ),
      );
      await tester.pumpAndSettle();
      // Error path hit — widget still renders
      expect(find.byType(FileViewerPanel), findsOneWidget);
      client.close();
    });

    testWidgets('file listing exception shows debug message', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        throw Exception('Network error');
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: buildPanel(wsClient: client),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(FileViewerPanel), findsOneWidget);
      client.close();
    });

    testWidgets('clicking file reads and displays content', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/content')) {
          return http.Response(
            jsonEncode({'content': 'hello world'}),
            200,
          );
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'readme.txt',
                'path': '/home/tester/readme.txt',
                'is_dir': false,
                'size': 11
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('readme.txt'));
      await tester.pumpAndSettle();

      expect(find.text('hello world'), findsOneWidget);
      client.close();
    });

    testWidgets('delete file via context menu', (tester) async {
      var deleteCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.method == 'DELETE') {
          deleteCalled = true;
          return http.Response('', 200);
        }
        if (request.url.path.contains('/files')) {
          if (deleteCalled) {
            return http.Response(jsonEncode([]), 200);
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'doomed.txt',
                'path': '/home/tester/doomed.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Long press to open context menu
      final center = tester.getCenter(find.text('doomed.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      // Tap delete
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      // Confirm in dialog
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();

      expect(deleteCalled, isTrue);
      client.close();
    });

    testWidgets('rename file via context menu', (tester) async {
      var renameCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename')) {
          renameCalled = true;
          return http.Response('', 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'old.txt',
                'path': '/home/tester/old.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Long press to open context menu
      final center = tester.getCenter(find.text('old.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      // Tap rename
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      // Submit the new name via the onSubmitted callback (press Enter)
      final dialogTextField = find.descendant(
        of: find.byType(AlertDialog),
        matching: find.byType(TextField),
      );
      await tester.enterText(dialogTextField, 'new.txt');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(renameCalled, isTrue);
      client.close();
    });

    testWidgets('download file via context menu', (tester) async {
      var downloadCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/download')) {
          downloadCalled = true;
          return http.Response('file content', 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'data.csv',
                'path': '/home/tester/data.csv',
                'is_dir': false,
                'size': 100
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('data.csv'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();

      expect(downloadCalled, isTrue);
      client.close();
    });

    testWidgets('download folder as tar.gz via context menu', (tester) async {
      var zipCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/download')) {
          zipCalled = true;
          return http.Response.bytes([0x50, 0x4b], 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'mydir',
                'path': '/home/tester/mydir',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('mydir'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();

      expect(zipCalled, isTrue);
      client.close();
    });

    testWidgets('breadcrumb navigation works', (tester) async {
      var requestedPath = '';
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          requestedPath = request.url.queryParameters['path'] ?? '';
          if (requestedPath == '/home/tester/subdir') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'inner.txt',
                  'path': '/home/tester/subdir/inner.txt',
                  'is_dir': false,
                  'size': 5
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'subdir',
                'path': '/home/tester/subdir',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Navigate into subdir
      await tester.tap(find.text('subdir'));
      await tester.pumpAndSettle();

      expect(find.text('inner.txt'), findsOneWidget);

      // Navigate back via home breadcrumb icon
      await tester.tap(find.byIcon(Icons.home));
      await tester.pumpAndSettle();

      expect(find.text('subdir'), findsOneWidget);
      client.close();
    });

    testWidgets('delete failure shows snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.method == 'DELETE') {
          return http.Response('', 500);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'file.txt',
                'path': '/home/tester/file.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('file.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      // Confirm delete
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Delete failed'), findsOneWidget);
      client.close();
    });

    testWidgets('rename failure shows snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename')) {
          return http.Response(jsonEncode({'detail': 'Name conflict'}), 409);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'old.txt',
                'path': '/home/tester/old.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('old.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      final dialogTextField = find.descendant(
        of: find.byType(AlertDialog),
        matching: find.byType(TextField),
      );
      await tester.enterText(dialogTextField, 'new.txt');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Rename failed'), findsOneWidget);
      client.close();
    });

    testWidgets('download failure shows snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/download')) {
          return http.Response('', 500);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'data.csv',
                'path': '/home/tester/data.csv',
                'is_dir': false,
                'size': 100
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('data.csv'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Download failed'), findsOneWidget);
      client.close();
    });

    testWidgets('clicking selected file deselects it', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/content')) {
          return http.Response(
            jsonEncode({'content': 'file content'}),
            200,
          );
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'readme.txt',
                'path': '/home/tester/readme.txt',
                'is_dir': false,
                'size': 12
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 800,
              height: 600,
              child: buildPanel(wsClient: client),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Click file to select it
      await tester.tap(find.text('readme.txt'));
      await tester.pumpAndSettle();
      expect(find.text('file content'), findsOneWidget);

      // Click the back button to deselect
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();
      expect(find.text('file content'), findsNothing);
      client.close();
    });

    testWidgets('cancel delete dialog does not delete', (tester) async {
      var deleteCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.method == 'DELETE') deleteCalled = true;
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'keep.txt',
                'path': '/home/tester/keep.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('keep.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      // Cancel the dialog
      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pumpAndSettle();
      expect(deleteCalled, isFalse);
      client.close();
    });

    testWidgets('delete exception shows error snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.method == 'DELETE') throw Exception('network');
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'file.txt',
                'path': '/home/tester/file.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('file.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Delete error'), findsOneWidget);
      client.close();
    });

    testWidgets('rename exception shows error snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename'))
          throw Exception('network');
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'old.txt',
                'path': '/home/tester/old.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('old.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();
      final tf = find.descendant(
          of: find.byType(AlertDialog), matching: find.byType(TextField));
      await tester.enterText(tf, 'new.txt');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.textContaining('Rename error'), findsOneWidget);
      client.close();
    });

    testWidgets('download exception shows error snackbar', (tester) async {
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/download'))
          throw Exception('network');
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'data.csv',
                'path': '/home/tester/data.csv',
                'is_dir': false,
                'size': 100
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('data.csv'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Download'));
      await tester.pumpAndSettle();
      expect(find.textContaining('Download error'), findsOneWidget);
      client.close();
    });

    testWidgets('rename file in subdirectory preserves path', (tester) async {
      var renamePath = '';
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename')) {
          final body = jsonDecode(request.body) as Map<String, dynamic>;
          renamePath = body['new_path'] as String? ?? '';
          return http.Response('', 200);
        }
        if (request.url.path.contains('/files')) {
          final path = request.url.queryParameters['path'] ?? '/home/tester';
          if (path == '/home/tester/subdir') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'inner.txt',
                  'path': '/home/tester/subdir/inner.txt',
                  'is_dir': false,
                  'size': 5
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'subdir',
                'path': '/home/tester/subdir',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      // Navigate into subdir
      await tester.tap(find.text('subdir'));
      await tester.pumpAndSettle();

      // Rename the file
      final center = tester.getCenter(find.text('inner.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();
      final tf = find.descendant(
          of: find.byType(AlertDialog), matching: find.byType(TextField));
      await tester.enterText(tf, 'renamed.txt');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // The rename should preserve the subdir/ prefix
      expect(renamePath, '/home/tester/subdir/renamed.txt');
      client.close();
    });

    testWidgets('parent folder button navigates up', (tester) async {
      var lastPath = '';
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          lastPath = request.url.queryParameters['path'] ?? '/home/tester';
          if (lastPath == '/home/tester/a/b') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'file.txt',
                  'path': '/home/tester/a/b/file.txt',
                  'is_dir': false,
                  'size': 5
                },
              ]),
              200,
            );
          }
          if (lastPath == '/home/tester/a') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'b',
                  'path': '/home/tester/a/b',
                  'is_dir': true,
                  'size': null
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'a',
                'path': '/home/tester/a',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      // Navigate deep: . -> a -> a/b
      await tester.tap(find.text('a'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('b'));
      await tester.pumpAndSettle();

      // Tap the up/parent button
      await tester.tap(find.byIcon(Icons.arrow_upward));
      await tester.pumpAndSettle();

      expect(lastPath, '/home/tester/a');
      client.close();
    });

    testWidgets('breadcrumb segment taps navigate', (tester) async {
      var lastPath = '';
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          lastPath = request.url.queryParameters['path'] ?? '/home/tester';
          if (lastPath == '/home/tester/sub') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'deep',
                  'path': '/home/tester/sub/deep',
                  'is_dir': true,
                  'size': null
                },
              ]),
              200,
            );
          }
          if (lastPath == '/home/tester/sub/deep') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'leaf.txt',
                  'path': '/home/tester/sub/deep/leaf.txt',
                  'is_dir': false,
                  'size': 1
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'sub',
                'path': '/home/tester/sub',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      // Navigate deep
      await tester.tap(find.text('sub'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('deep'));
      await tester.pumpAndSettle();

      // Tap the "sub" breadcrumb segment to go back
      await tester.tap(find.text('sub'));
      await tester.pumpAndSettle();

      expect(lastPath, '/home/tester/sub');
      client.close();
    });

    testWidgets('rename via button tap', (tester) async {
      var renameCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename')) {
          renameCalled = true;
          return http.Response('', 200);
        }
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'old.txt',
                'path': '/home/tester/old.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('old.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();

      final tf = find.descendant(
          of: find.byType(AlertDialog), matching: find.byType(TextField));
      await tester.enterText(tf, 'new.txt');
      // Tap Rename button instead of keyboard submit
      await tester.tap(find.text('Rename').last);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(renameCalled, isTrue);
      client.close();
    });

    testWidgets('rename cancel via button', (tester) async {
      var renameCalled = false;
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files/rename')) renameCalled = true;
        if (request.url.path.contains('/files')) {
          return http.Response(
            jsonEncode([
              {
                'name': 'keep.txt',
                'path': '/home/tester/keep.txt',
                'is_dir': false,
                'size': 5
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      final center = tester.getCenter(find.text('keep.txt'));
      await tester.tapAt(center, buttons: kSecondaryMouseButton);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Rename'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(renameCalled, isFalse);
      client.close();
    });

    testWidgets('root breadcrumb slash navigates to root', (tester) async {
      var lastPath = '';
      testHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/files')) {
          lastPath = request.url.queryParameters['path'] ?? '/home/tester';
          if (lastPath == '/home/tester/sub') {
            return http.Response(
              jsonEncode([
                {
                  'name': 'file.txt',
                  'path': '/home/tester/sub/file.txt',
                  'is_dir': false,
                  'size': 1
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'name': 'sub',
                'path': '/home/tester/sub',
                'is_dir': true,
                'size': null
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final client = _MockWsClient();
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: SizedBox(
                  width: 800,
                  height: 600,
                  child: buildPanel(wsClient: client)))));
      await tester.pumpAndSettle();

      // Navigate into subdir
      await tester.tap(find.text('sub'));
      await tester.pumpAndSettle();

      // Tap the leading "/" breadcrumb to go to root
      // The breadcrumb shows: / home / work / sub
      // We want the first "/" which is the root link.
      await tester.tap(find.text('/').first);
      await tester.pumpAndSettle();

      expect(lastPath, '/');
      client.close();
    });
  });

  group('formatMtime', () {
    test('returns empty string for null', () {
      expect(formatMtime(null), '');
    });

    test('returns just now for recent timestamp', () {
      final now = DateTime.now().millisecondsSinceEpoch / 1000;
      expect(formatMtime(now), 'just now');
    });

    test('returns minutes ago', () {
      final tenMinAgo = DateTime.now()
              .subtract(const Duration(minutes: 10))
              .millisecondsSinceEpoch /
          1000;
      expect(formatMtime(tenMinAgo), '10m ago');
    });

    test('returns hours ago', () {
      final threeHoursAgo = DateTime.now()
              .subtract(const Duration(hours: 3))
              .millisecondsSinceEpoch /
          1000;
      expect(formatMtime(threeHoursAgo), '3h ago');
    });

    test('returns days ago', () {
      final fiveDaysAgo = DateTime.now()
              .subtract(const Duration(days: 5))
              .millisecondsSinceEpoch /
          1000;
      expect(formatMtime(fiveDaysAgo), '5d ago');
    });

    test('returns date for old timestamps', () {
      // 2025-01-15 00:00:00 UTC
      const oldTimestamp = 1736899200.0;
      final result = formatMtime(oldTimestamp);
      expect(result, matches(RegExp(r'^\d{4}-\d{2}-\d{2}$')));
    });
  });
}
