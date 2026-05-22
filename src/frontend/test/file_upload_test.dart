import 'dart:async';
import 'dart:typed_data';

import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/file_viewer/file_upload.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

Widget buildDropZone({
  String workspaceId = 'ws-1',
  String? authToken = 'test-token',
  String currentPath = '.',
  List<Map<String, dynamic>> currentEntries = const [],
  VoidCallback? onUploadComplete,
  GlobalKey<FileDropZoneState>? key,
}) {
  return MaterialApp(
    home: Scaffold(
      body: FileDropZone(
        key: key,
        workspaceId: workspaceId,
        authToken: authToken,
        currentPath: currentPath,
        currentEntries: currentEntries,
        onUploadComplete: onUploadComplete ?? () {},
        child: const Text('DROP_HERE'),
      ),
    ),
  );
}

DropDoneDetails makeDropDetails(List<DropItem> files) {
  return DropDoneDetails(
    files: files,
    localPosition: Offset.zero,
    globalPosition: Offset.zero,
  );
}

/// Create a DropItemFile with a path so that .name works in io mode.
DropItemFile makeFile(String name, List<int> bytes) {
  return DropItemFile.fromData(
    Uint8List.fromList(bytes),
    path: '/tmp/$name',
  );
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    testUploadOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testUploadOverride = null;
  });

  group('FileDropZone', () {
    testWidgets('renders child widget', (tester) async {
      await tester.pumpWidget(buildDropZone());
      expect(find.text('DROP_HERE'), findsOneWidget);
    });

    testWidgets('contains a DropTarget', (tester) async {
      await tester.pumpWidget(buildDropZone());
      expect(find.byType(DropTarget), findsOneWidget);
    });

    testWidgets('does not show drag overlay initially', (tester) async {
      await tester.pumpWidget(buildDropZone());
      expect(find.text('Drop files or folders to upload'), findsNothing);
    });

    testWidgets('does not show upload progress initially', (tester) async {
      await tester.pumpWidget(buildDropZone());
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    group('upload via uploadFiles', () {
      testWidgets('calls onUploadComplete after upload', (tester) async {
        bool completed = false;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async => 200;

        await tester.pumpWidget(buildDropZone(
          key: key,
          onUploadComplete: () => completed = true,
        ));

        final file = makeFile('test.txt', [1, 2, 3]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        // Let the upload complete and the 500ms delay pass
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(completed, isTrue);
      });

      testWidgets('sends correct URL with workspace and path', (tester) async {
        String? capturedUrl;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          capturedUrl = url;
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          workspaceId: 'my-ws',
          currentPath: 'src/lib',
        ));

        final file = makeFile('hello.dart', [10, 20]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(capturedUrl, contains('/workspaces/my-ws/files/upload'));
        expect(capturedUrl, contains('path=src%2Flib%2Fhello.dart'));
      });

      testWidgets('root path does not prefix with dot', (tester) async {
        String? capturedUrl;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          capturedUrl = url;
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          currentPath: '.',
        ));

        final file = makeFile('root.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(capturedUrl, contains('path=root.txt'));
        expect(capturedUrl, isNot(contains('.%2F')));
      });

      testWidgets('sends auth header when token present', (tester) async {
        Map<String, String>? capturedHeaders;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          capturedHeaders = headers;
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          authToken: 'my-secret-token',
        ));

        final file = makeFile('a.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(capturedHeaders?['Authorization'], 'Bearer my-secret-token');
      });

      testWidgets('no auth header when token is null', (tester) async {
        Map<String, String>? capturedHeaders;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          capturedHeaders = headers;
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          authToken: null,
        ));

        final file = makeFile('a.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(capturedHeaders?.containsKey('Authorization'), isNot(isTrue));
      });

      testWidgets('uploads multiple files sequentially', (tester) async {
        final uploadedFiles = <String>[];
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          uploadedFiles.add(filename);
          return 200;
        };

        await tester.pumpWidget(buildDropZone(key: key));

        final files = [
          makeFile('a.txt', [1]),
          makeFile('b.txt', [2]),
          makeFile('c.txt', [3]),
        ];
        key.currentState!.uploadFiles(makeDropDetails(files));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(uploadedFiles, ['a.txt', 'b.txt', 'c.txt']);
      });

      testWidgets('sends correct file bytes', (tester) async {
        List<int>? capturedBytes;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          capturedBytes = bytes;
          return 200;
        };

        await tester.pumpWidget(buildDropZone(key: key));

        final content = [72, 101, 108, 108, 111]; // "Hello"
        final file = makeFile('hello.txt', content);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(capturedBytes, content);
      });

      testWidgets('shows upload progress during upload', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        final completer = Completer<int>();

        testUploadOverride = (url, headers, filename, bytes) async {
          return completer.future;
        };

        await tester.pumpWidget(buildDropZone(key: key));

        final files = [
          makeFile('a.txt', [1]),
          makeFile('b.txt', [2]),
        ];

        key.currentState!.uploadFiles(makeDropDetails(files));
        await tester.pump();

        expect(find.byType(CircularProgressIndicator), findsOneWidget);
        expect(find.textContaining('Uploading'), findsOneWidget);

        // Complete the upload so the test can finish cleanly
        completer.complete(200);
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));
      });

      testWidgets('handles upload failure gracefully', (tester) async {
        bool completed = false;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async => 500;

        await tester.pumpWidget(buildDropZone(
          key: key,
          onUploadComplete: () => completed = true,
        ));

        final file = makeFile('fail.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(completed, isTrue);
      });

      testWidgets('handles upload exception gracefully', (tester) async {
        bool completed = false;
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async {
          throw Exception('network error');
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          onUploadComplete: () => completed = true,
        ));

        final file = makeFile('error.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(completed, isTrue);
      });
    });

    group('conflict detection', () {
      testWidgets('shows snackbar for single conflict', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        bool completed = false;

        testUploadOverride = (url, headers, filename, bytes) async => 200;

        await tester.pumpWidget(buildDropZone(
          key: key,
          currentEntries: [
            {'name': 'existing.txt', 'type': 'file'},
          ],
          onUploadComplete: () => completed = true,
        ));

        final file = makeFile('existing.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();

        expect(find.text('Already exists: existing.txt'), findsOneWidget);
        expect(completed, isFalse);
      });

      testWidgets('shows snackbar for multiple conflicts', (tester) async {
        final key = GlobalKey<FileDropZoneState>();

        testUploadOverride = (url, headers, filename, bytes) async => 200;

        await tester.pumpWidget(buildDropZone(
          key: key,
          currentEntries: [
            {'name': 'a.txt', 'type': 'file'},
            {'name': 'b.txt', 'type': 'file'},
          ],
        ));

        final files = [
          makeFile('a.txt', [1]),
          makeFile('b.txt', [2]),
        ];
        key.currentState!.uploadFiles(makeDropDetails(files));
        await tester.pump();

        expect(find.textContaining('Already exist:'), findsOneWidget);
        expect(find.textContaining('a.txt'), findsOneWidget);
        expect(find.textContaining('b.txt'), findsOneWidget);
      });

      testWidgets('does not upload when conflicts exist', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        final uploaded = <String>[];

        testUploadOverride = (url, headers, filename, bytes) async {
          uploaded.add(filename);
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          currentEntries: [
            {'name': 'conflict.txt', 'type': 'file'},
          ],
        ));

        final file = makeFile('conflict.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(uploaded, isEmpty);
      });

      testWidgets('allows upload when no conflicts', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        final uploaded = <String>[];

        testUploadOverride = (url, headers, filename, bytes) async {
          uploaded.add(filename);
          return 200;
        };

        await tester.pumpWidget(buildDropZone(
          key: key,
          currentEntries: [
            {'name': 'other.txt', 'type': 'file'},
          ],
        ));

        final file = makeFile('new.txt', [1]);
        key.currentState!.uploadFiles(makeDropDetails([file]));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 600));

        expect(uploaded, ['new.txt']);
      });
    });

    group('collectFiles', () {
      testWidgets('flattens nested directories', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        await tester.pumpWidget(buildDropZone(key: key));

        final innerFile = makeFile('deep.txt', [1]);
        final dir = DropItemDirectory(
          '/tmp/subdir',
          [innerFile],
          name: 'subdir',
        );

        final result = key.currentState!.collectFiles([dir], '');
        expect(result.length, 1);
        expect(result[0].$1, 'subdir/deep.txt');
      });

      testWidgets('handles flat file list', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        await tester.pumpWidget(buildDropZone(key: key));

        final files = [
          makeFile('a.txt', [1]),
          makeFile('b.txt', [2]),
        ];

        final result = key.currentState!.collectFiles(files, '');
        expect(result.length, 2);
        expect(result[0].$1, 'a.txt');
        expect(result[1].$1, 'b.txt');
      });

      testWidgets('preserves prefix in paths', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        await tester.pumpWidget(buildDropZone(key: key));

        final file = makeFile('file.txt', [1]);

        final result = key.currentState!.collectFiles([file], 'parent');
        expect(result[0].$1, 'parent/file.txt');
      });

      testWidgets('handles deeply nested directories', (tester) async {
        final key = GlobalKey<FileDropZoneState>();
        await tester.pumpWidget(buildDropZone(key: key));

        final deepFile = makeFile('leaf.txt', [1]);
        final inner = DropItemDirectory('/tmp/c', [deepFile], name: 'c');
        final middle = DropItemDirectory('/tmp/b', [inner], name: 'b');
        final outer = DropItemDirectory('/tmp/a', [middle], name: 'a');

        final result = key.currentState!.collectFiles([outer], '');
        expect(result.length, 1);
        expect(result[0].$1, 'a/b/c/leaf.txt');
      });
    });
  });

  group('FileDropZone drag events', () {
    testWidgets('drag enter shows overlay, drag exit hides it', (tester) async {
      await tester.pumpWidget(buildDropZone());
      await tester.pumpAndSettle();

      // No overlay initially
      expect(find.text('Drop files or folders to upload'), findsNothing);

      // Simulate drag enter
      final dropTarget = tester.widget<DropTarget>(find.byType(DropTarget));
      dropTarget.onDragEntered!(DropEventDetails(
        localPosition: Offset.zero,
        globalPosition: Offset.zero,
      ));
      await tester.pump();

      expect(find.text('Drop files or folders to upload'), findsOneWidget);

      // Simulate drag exit
      dropTarget.onDragExited!(DropEventDetails(
        localPosition: Offset.zero,
        globalPosition: Offset.zero,
      ));
      await tester.pump();

      expect(find.text('Drop files or folders to upload'), findsNothing);
    });

    testWidgets('drag done triggers upload', (tester) async {
      testUploadOverride = (url, headers, name, bytes) async => 200;
      final key = GlobalKey<FileDropZoneState>();
      var completed = false;

      await tester.pumpWidget(buildDropZone(
        key: key,
        onUploadComplete: () => completed = true,
      ));
      await tester.pumpAndSettle();

      final dropTarget = tester.widget<DropTarget>(find.byType(DropTarget));
      dropTarget.onDragDone!(makeDropDetails([
        makeFile('test.txt', [1, 2, 3]),
      ]));
      await tester.pump();
      await tester.pump(const Duration(seconds: 1));

      expect(completed, isTrue);
    });
  });
}
