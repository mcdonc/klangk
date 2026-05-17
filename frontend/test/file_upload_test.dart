import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/file_viewer/file_upload.dart';
import 'package:bark_frontend/utils/backend_url.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('FileDropZone', () {
    testWidgets('renders child widget', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileDropZone(
              workspaceId: 'ws-1',
              authToken: 'token',
              currentPath: '.',
              currentEntries: const [],
              onUploadComplete: () {},
              child: const Text('CHILD_CONTENT'),
            ),
          ),
        ),
      );

      expect(find.text('CHILD_CONTENT'), findsOneWidget);
    });

    testWidgets('renders with empty entries', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: FileDropZone(
              workspaceId: 'ws-1',
              authToken: 'token',
              currentPath: '.',
              currentEntries: const [],
              onUploadComplete: () {},
              child: const Text('DROP_HERE'),
            ),
          ),
        ),
      );

      expect(find.byType(FileDropZone), findsOneWidget);
      expect(find.text('DROP_HERE'), findsOneWidget);
    });
  });
}
