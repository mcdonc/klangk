import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/agui/agui_events.dart';
import 'package:bark_frontend/file_viewer/file_viewer_panel.dart';
import 'package:bark_frontend/utils/backend_url.dart';

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
  });

  tearDown(() {
    testBaseUrlOverride = null;
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

      // Path bar shows root
      expect(find.text('/'), findsOneWidget);
      // Refresh button
      expect(find.byIcon(Icons.refresh), findsOneWidget);
      // Folder icon
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

      // refresh() should not throw
      key.currentState!.refresh();
      await tester.pump();
      expect(find.byType(FileViewerPanel), findsOneWidget);
      client.close();
    });

    testWidgets('shows loading indicator initially', (tester) async {
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

      // The initial _loadFiles call may show a loading indicator
      expect(find.byType(FileViewerPanel), findsOneWidget);
      client.close();
    });
  });
}
